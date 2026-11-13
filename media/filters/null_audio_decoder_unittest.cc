// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "media/filters/null_audio_decoder.h"

#include <array>
#include <utility>
#include <vector>

#include "base/functional/bind.h"
#include "base/functional/callback_helpers.h"
#include "base/test/task_environment.h"
#include "base/time/time.h"
#include "media/base/audio_buffer.h"
#include "media/base/audio_timestamp_helper.h"
#include "media/base/decoder_buffer.h"
#include "media/base/test_helpers.h"
#include "media/base/timestamp_constants.h"
#include "testing/gtest/include/gtest/gtest.h"

namespace media {

class NullAudioDecoderTest : public testing::Test {
 public:
  void Initialize(const AudioDecoderConfig& config,
                  DecoderStatus::Codes expected_status) {
    bool callback_called = false;
    decoder_.Initialize(
        config, /*cdm_context=*/nullptr,
        base::BindOnce(
            [](bool* callback_called, DecoderStatus::Codes expected_status,
               DecoderStatus status) {
              *callback_called = true;
              EXPECT_EQ(status.code(), expected_status);
            },
            &callback_called, expected_status),
        base::BindRepeating(&NullAudioDecoderTest::OnBuffer,
                            base::Unretained(this)),
        base::NullCallback());

    EXPECT_FALSE(callback_called);
    task_environment_.RunUntilIdle();
    EXPECT_TRUE(callback_called);
  }

  DecoderStatus Decode(base::TimeDelta timestamp,
                       base::TimeDelta duration = kNoTimestamp) {
    constexpr std::array<uint8_t, 1> kData = {0};
    scoped_refptr<DecoderBuffer> buffer = DecoderBuffer::CopyFrom(kData);
    buffer->set_timestamp(timestamp);
    buffer->set_duration(duration);
    return DecodeBuffer(std::move(buffer));
  }

  DecoderStatus DecodeBuffer(scoped_refptr<DecoderBuffer> buffer) {
    bool callback_called = false;
    DecoderStatus decode_status(DecoderStatus::Codes::kFailed);
    decoder_.Decode(std::move(buffer),
                    base::BindOnce(
                        [](bool* callback_called, DecoderStatus* decode_status,
                           DecoderStatus status) {
                          *callback_called = true;
                          *decode_status = std::move(status);
                        },
                        &callback_called, &decode_status));

    EXPECT_FALSE(callback_called);
    task_environment_.RunUntilIdle();
    EXPECT_TRUE(callback_called);
    return decode_status;
  }

  void OnBuffer(scoped_refptr<AudioBuffer> buffer) {
    buffers_.push_back(std::move(buffer));
  }

 protected:
  base::test::SingleThreadTaskEnvironment task_environment_;
  NullAudioDecoder decoder_;
  std::vector<scoped_refptr<AudioBuffer>> buffers_;
};

TEST_F(NullAudioDecoderTest, AcceptsClearAudioConfig) {
  Initialize(TestAudioConfig::Normal(), DecoderStatus::Codes::kOk);

  EXPECT_EQ(decoder_.GetDecoderType(), AudioDecoderType::kNull);
  // This ID was shipped in the M150 releases and is persisted in telemetry.
  EXPECT_EQ(static_cast<int>(decoder_.GetDecoderType()), 12);
  EXPECT_FALSE(decoder_.IsPlatformDecoder());
  EXPECT_FALSE(decoder_.SupportsDecryption());
}

TEST_F(NullAudioDecoderTest, RejectsEncryptedConfig) {
  Initialize(TestAudioConfig::NormalEncrypted(),
             DecoderStatus::Codes::kUnsupportedEncryptionMode);
  EXPECT_TRUE(buffers_.empty());
}

TEST_F(NullAudioDecoderTest, EmitsAllocationFreeSilentBufferWithExactTiming) {
  const AudioDecoderConfig config = TestAudioConfig::Normal();
  Initialize(config, DecoderStatus::Codes::kOk);

  constexpr base::TimeDelta kTimestamp = base::Milliseconds(17);
  constexpr base::TimeDelta kDuration = base::Milliseconds(20);
  EXPECT_TRUE(Decode(kTimestamp, kDuration).is_ok());

  ASSERT_EQ(buffers_.size(), 1u);
  EXPECT_EQ(buffers_[0]->timestamp(), kTimestamp);
  EXPECT_EQ(buffers_[0]->frame_count(),
            AudioTimestampHelper::TimeToFrames(kDuration,
                                               config.samples_per_second()));
  EXPECT_EQ(buffers_[0]->sample_rate(), config.samples_per_second());
  EXPECT_EQ(buffers_[0]->channel_count(), config.channels());
  EXPECT_EQ(buffers_[0]->channel_layout(), config.channel_layout());
  EXPECT_EQ(buffers_[0]->data_size(), 0u);
}

TEST_F(NullAudioDecoderTest, InfersUnknownDurationFromNextTimestamp) {
  const AudioDecoderConfig config = TestAudioConfig::Normal();
  Initialize(config, DecoderStatus::Codes::kOk);

  constexpr base::TimeDelta kFirstTimestamp = base::Milliseconds(100);
  constexpr base::TimeDelta kSecondTimestamp = base::Milliseconds(125);
  EXPECT_TRUE(Decode(kFirstTimestamp).is_ok());
  EXPECT_TRUE(buffers_.empty());

  EXPECT_TRUE(Decode(kSecondTimestamp).is_ok());
  ASSERT_EQ(buffers_.size(), 1u);
  EXPECT_EQ(buffers_[0]->timestamp(), kFirstTimestamp);
  EXPECT_EQ(buffers_[0]->frame_count(), AudioTimestampHelper::TimeToFrames(
                                            kSecondTimestamp - kFirstTimestamp,
                                            config.samples_per_second()));

  EXPECT_TRUE(DecodeBuffer(DecoderBuffer::CreateEOSBuffer()).is_ok());
  ASSERT_EQ(buffers_.size(), 2u);
  EXPECT_EQ(buffers_[1]->frame_count(), buffers_[0]->frame_count());
}

TEST_F(NullAudioDecoderTest, EndOfStreamFlushesOnlyPendingMetadata) {
  const AudioDecoderConfig config = TestAudioConfig::Normal();
  Initialize(config, DecoderStatus::Codes::kOk);

  EXPECT_TRUE(Decode(base::Milliseconds(10)).is_ok());
  EXPECT_TRUE(buffers_.empty());
  EXPECT_TRUE(DecodeBuffer(DecoderBuffer::CreateEOSBuffer()).is_ok());

  ASSERT_EQ(buffers_.size(), 1u);
  EXPECT_EQ(buffers_[0]->frame_count(),
            AudioTimestampHelper::TimeToFrames(base::Milliseconds(20),
                                               config.samples_per_second()));

  EXPECT_TRUE(Decode(base::Milliseconds(30), base::Milliseconds(20)).is_ok());
  EXPECT_EQ(buffers_.size(), 1u);
}

TEST_F(NullAudioDecoderTest, RejectsMissingTimestamp) {
  Initialize(TestAudioConfig::Normal(), DecoderStatus::Codes::kOk);
  EXPECT_EQ(Decode(kNoTimestamp, base::Milliseconds(20)).code(),
            DecoderStatus::Codes::kFailed);
  EXPECT_TRUE(buffers_.empty());
}

TEST_F(NullAudioDecoderTest, ResetClearsPendingAndEndOfStreamState) {
  Initialize(TestAudioConfig::Normal(), DecoderStatus::Codes::kOk);
  EXPECT_TRUE(Decode(base::Milliseconds(10)).is_ok());
  EXPECT_TRUE(DecodeBuffer(DecoderBuffer::CreateEOSBuffer()).is_ok());
  ASSERT_EQ(buffers_.size(), 1u);

  bool reset_called = false;
  decoder_.Reset(base::BindOnce(
      [](bool* reset_called) { *reset_called = true; }, &reset_called));
  EXPECT_FALSE(reset_called);
  task_environment_.RunUntilIdle();
  EXPECT_TRUE(reset_called);

  EXPECT_TRUE(Decode(base::Milliseconds(100), base::Milliseconds(10)).is_ok());
  ASSERT_EQ(buffers_.size(), 2u);
  EXPECT_EQ(buffers_[1]->timestamp(), base::Milliseconds(100));
}

TEST_F(NullAudioDecoderTest, ReinitializeUpdatesSampleRate) {
  Initialize(TestAudioConfig::Normal(), DecoderStatus::Codes::kOk);
  EXPECT_TRUE(Decode(base::Milliseconds(0), base::Milliseconds(10)).is_ok());

  const AudioDecoderConfig high_rate = TestAudioConfig::HighSampleRate();
  Initialize(high_rate, DecoderStatus::Codes::kOk);
  EXPECT_TRUE(Decode(base::Milliseconds(20), base::Milliseconds(10)).is_ok());

  ASSERT_EQ(buffers_.size(), 2u);
  EXPECT_EQ(buffers_[1]->sample_rate(), high_rate.samples_per_second());
  EXPECT_EQ(buffers_[1]->frame_count(),
            AudioTimestampHelper::TimeToFrames(base::Milliseconds(10),
                                               high_rate.samples_per_second()));
}

}  // namespace media

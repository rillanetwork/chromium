// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "media/filters/null_video_decoder.h"

#include <array>
#include <utility>
#include <vector>

#include "base/functional/bind.h"
#include "base/functional/callback_helpers.h"
#include "base/run_loop.h"
#include "base/test/task_environment.h"
#include "base/time/time.h"
#include "media/base/decoder_buffer.h"
#include "media/base/test_helpers.h"
#include "media/base/video_frame.h"
#include "testing/gtest/include/gtest/gtest.h"

namespace media {

class NullVideoDecoderTest : public testing::Test {
 public:
  void Initialize(const VideoDecoderConfig& config,
                  DecoderStatus::Codes expected_status) {
    bool callback_called = false;
    decoder_.Initialize(
        config, /*low_delay=*/false, /*cdm_context=*/nullptr,
        base::BindOnce(
            [](bool* callback_called, DecoderStatus::Codes expected_status,
               DecoderStatus status) {
              *callback_called = true;
              EXPECT_EQ(status.code(), expected_status);
            },
            &callback_called, expected_status),
        base::BindRepeating(&NullVideoDecoderTest::OnFrame,
                            base::Unretained(this)),
        base::NullCallback());

    EXPECT_FALSE(callback_called);
    task_environment_.RunUntilIdle();
    EXPECT_TRUE(callback_called);
  }

  DecoderStatus Decode(base::TimeDelta timestamp) {
    constexpr std::array<uint8_t, 1> kData = {0};
    scoped_refptr<DecoderBuffer> buffer = DecoderBuffer::CopyFrom(kData);
    buffer->set_timestamp(timestamp);
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

  void OnFrame(scoped_refptr<VideoFrame> frame) {
    frames_.push_back(std::move(frame));
  }

 protected:
  base::test::SingleThreadTaskEnvironment task_environment_;
  NullVideoDecoder decoder_;
  std::vector<scoped_refptr<VideoFrame>> frames_;
};

TEST_F(NullVideoDecoderTest, AcceptsClearVideoConfigs) {
  for (VideoCodec codec :
       {VideoCodec::kH264, VideoCodec::kHEVC, VideoCodec::kAV1,
        VideoCodec::kVP8, VideoCodec::kVP9}) {
    Initialize(TestVideoConfig::Normal(codec), DecoderStatus::Codes::kOk);
  }

  EXPECT_EQ(decoder_.GetDecoderType(), VideoDecoderType::kNull);
  EXPECT_FALSE(decoder_.IsPlatformDecoder());
  EXPECT_FALSE(decoder_.SupportsDecryption());
  EXPECT_EQ(decoder_.GetMaxDecodeRequests(), 1);
}

TEST_F(NullVideoDecoderTest, RejectsEncryptedConfig) {
  Initialize(TestVideoConfig::NormalEncrypted(VideoCodec::kH264),
             DecoderStatus::Codes::kUnsupportedEncryptionMode);
  EXPECT_TRUE(frames_.empty());
}

TEST_F(NullVideoDecoderTest, EmitsTimestampedSharedBlackFramesWithConfigSize) {
  const VideoDecoderConfig config = TestVideoConfig::NormalWithColorSpace(
      VideoCodec::kH264, VideoColorSpace::REC709());
  Initialize(config, DecoderStatus::Codes::kOk);

  constexpr base::TimeDelta kFirstTimestamp = base::Milliseconds(17);
  constexpr base::TimeDelta kSecondTimestamp = base::Milliseconds(50);
  EXPECT_TRUE(Decode(kFirstTimestamp).is_ok());
  EXPECT_TRUE(Decode(kSecondTimestamp).is_ok());

  ASSERT_EQ(frames_.size(), 2u);
  EXPECT_NE(frames_[0].get(), frames_[1].get());
  EXPECT_EQ(frames_[0]->timestamp(), kFirstTimestamp);
  EXPECT_EQ(frames_[1]->timestamp(), kSecondTimestamp);
  EXPECT_EQ(frames_[0]->format(), PIXEL_FORMAT_I420);
  EXPECT_EQ(frames_[0]->coded_size(), config.coded_size());
  EXPECT_EQ(frames_[0]->visible_rect(), config.visible_rect());
  EXPECT_EQ(frames_[0]->natural_size(), config.natural_size());
  EXPECT_EQ(frames_[0]->ColorSpace(),
            config.color_space_info().ToGfxColorSpace());
  EXPECT_EQ(frames_[0]->hdr_metadata(), config.hdr_metadata());

  EXPECT_EQ(frames_[0]->data(VideoFrame::Plane::kY),
            frames_[1]->data(VideoFrame::Plane::kY));
  EXPECT_EQ(frames_[0]->data(VideoFrame::Plane::kU),
            frames_[1]->data(VideoFrame::Plane::kU));
  EXPECT_EQ(frames_[0]->data(VideoFrame::Plane::kV),
            frames_[1]->data(VideoFrame::Plane::kV));
  EXPECT_EQ(frames_[0]->data(VideoFrame::Plane::kY)[0], 0u);
  EXPECT_EQ(frames_[0]->data(VideoFrame::Plane::kU)[0], 0x80u);
  EXPECT_EQ(frames_[0]->data(VideoFrame::Plane::kV)[0], 0x80u);
}

TEST_F(NullVideoDecoderTest, EndOfStreamDoesNotEmitFrame) {
  Initialize(TestVideoConfig::Normal(VideoCodec::kAV1),
             DecoderStatus::Codes::kOk);
  EXPECT_TRUE(Decode(base::Milliseconds(10)).is_ok());
  ASSERT_EQ(frames_.size(), 1u);

  EXPECT_TRUE(DecodeBuffer(DecoderBuffer::CreateEOSBuffer()).is_ok());
  EXPECT_EQ(frames_.size(), 1u);

  EXPECT_TRUE(Decode(base::Milliseconds(20)).is_ok());
  EXPECT_EQ(frames_.size(), 1u);
}

TEST_F(NullVideoDecoderTest, ReinitializeUpdatesNaturalSize) {
  Initialize(TestVideoConfig::Normal(VideoCodec::kH264),
             DecoderStatus::Codes::kOk);
  EXPECT_TRUE(Decode(base::Milliseconds(10)).is_ok());

  const VideoDecoderConfig large_config =
      TestVideoConfig::Large(VideoCodec::kAV1);
  Initialize(large_config, DecoderStatus::Codes::kOk);
  EXPECT_TRUE(Decode(base::Milliseconds(20)).is_ok());

  ASSERT_EQ(frames_.size(), 2u);
  EXPECT_EQ(frames_[1]->natural_size(), large_config.natural_size());
  EXPECT_EQ(frames_[1]->coded_size(), large_config.coded_size());
  EXPECT_EQ(frames_[1]->visible_rect(), large_config.visible_rect());
}

TEST_F(NullVideoDecoderTest, ResetCallbackIsAsynchronous) {
  Initialize(TestVideoConfig::Normal(VideoCodec::kVP9),
             DecoderStatus::Codes::kOk);

  bool callback_called = false;
  decoder_.Reset(
      base::BindOnce([](bool* callback_called) { *callback_called = true; },
                     &callback_called));
  EXPECT_FALSE(callback_called);
  task_environment_.RunUntilIdle();
  EXPECT_TRUE(callback_called);
}

TEST_F(NullVideoDecoderTest, ResetClearsEndOfStreamState) {
  Initialize(TestVideoConfig::Normal(VideoCodec::kVP9),
             DecoderStatus::Codes::kOk);
  EXPECT_TRUE(DecodeBuffer(DecoderBuffer::CreateEOSBuffer()).is_ok());

  decoder_.Reset(base::DoNothing());
  task_environment_.RunUntilIdle();
  EXPECT_TRUE(Decode(base::Milliseconds(20)).is_ok());
  EXPECT_EQ(frames_.size(), 1u);
}

}  // namespace media

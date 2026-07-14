// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "media/renderers/default_decoder_factory.h"

#include <algorithm>
#include <memory>
#include <vector>

#include "base/functional/callback_helpers.h"
#include "base/test/scoped_feature_list.h"
#include "base/test/task_environment.h"
#include "media/base/audio_decoder.h"
#include "media/base/media_switches.h"
#include "media/base/media_util.h"
#include "media/base/video_decoder.h"
#include "media/media_buildflags.h"
#include "testing/gtest/include/gtest/gtest.h"
#include "ui/gfx/color_space.h"

namespace media {

class DefaultDecoderFactoryTest : public testing::Test {
 protected:
  bool CreatesIamfAudioDecoder() {
    DefaultDecoderFactory factory(/*external_decoder_factory=*/nullptr);
    std::vector<std::unique_ptr<AudioDecoder>> decoders;
    factory.CreateAudioDecoders(task_environment_.GetMainThreadTaskRunner(),
                                &media_log_, &decoders);
    return std::ranges::any_of(decoders, [](const auto& decoder) {
      return decoder->GetDecoderType() == AudioDecoderType::kIamf;
    });
  }

 private:
  base::test::TaskEnvironment task_environment_;
  NullMediaLog media_log_;
};

TEST_F(DefaultDecoderFactoryTest, IamfDecoderTracksFeatureState) {
  {
    base::test::ScopedFeatureList scoped_feature_list;
    scoped_feature_list.InitAndDisableFeature(kIamfAudioDecoding);
    EXPECT_FALSE(CreatesIamfAudioDecoder());
  }

  {
    base::test::ScopedFeatureList scoped_feature_list;
    scoped_feature_list.InitAndEnableFeature(kIamfAudioDecoding);
    EXPECT_TRUE(CreatesIamfAudioDecoder());
  }
}

#if BUILDFLAG(ENABLE_SIMULATED_VIDEO_DECODE)
TEST_F(DefaultDecoderFactoryTest, SimulatedBuildExposesOnlyNullVideoDecoder) {
  base::test::SingleThreadTaskEnvironment task_environment;
  NullMediaLog media_log;
  DefaultDecoderFactory factory(/*external_decoder_factory=*/nullptr);
  std::vector<std::unique_ptr<VideoDecoder>> decoders;

  factory.CreateVideoDecoders(
      base::SequencedTaskRunner::GetCurrentDefault(), /*gpu_factories=*/nullptr,
      &media_log, base::NullCallback(), gfx::ColorSpace(), &decoders);

  ASSERT_EQ(decoders.size(), 1u);
  EXPECT_EQ(decoders.front()->GetDecoderType(), VideoDecoderType::kNull);
  EXPECT_FALSE(decoders.front()->IsPlatformDecoder());
  EXPECT_FALSE(decoders.front()->SupportsDecryption());
}
#endif

}  // namespace media

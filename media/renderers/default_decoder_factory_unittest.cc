// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "media/renderers/default_decoder_factory.h"

#include <memory>
#include <vector>

#include "base/functional/callback_helpers.h"
#include "base/test/task_environment.h"
#include "media/base/decoder.h"
#include "media/base/media_util.h"
#include "media/base/video_decoder.h"
#include "testing/gtest/include/gtest/gtest.h"
#include "ui/gfx/color_space.h"

namespace media {

TEST(DefaultDecoderFactoryTest, SimulatedBuildExposesOnlyNullVideoDecoder) {
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

}  // namespace media

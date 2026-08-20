// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "media/filters/null_video_decoder.h"

#include <utility>

#include "base/check.h"
#include "base/task/bind_post_task.h"
#include "media/base/decoder_buffer.h"

namespace media {

NullVideoDecoder::NullVideoDecoder() {
  DETACH_FROM_SEQUENCE(sequence_checker_);
}

NullVideoDecoder::~NullVideoDecoder() {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
}

VideoDecoderType NullVideoDecoder::GetDecoderType() const {
  return VideoDecoderType::kNull;
}

void NullVideoDecoder::Initialize(const VideoDecoderConfig& config,
                                  bool /*low_delay*/,
                                  CdmContext* /*cdm_context*/,
                                  InitCB init_cb,
                                  const OutputCB& output_cb,
                                  const WaitingCB& /*waiting_cb*/) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  DCHECK(config.IsValidConfig());
  DCHECK(output_cb);

  InitCB bound_init_cb = base::BindPostTaskToCurrentDefault(std::move(init_cb));

  initialized_ = false;
  output_cb_.Reset();
  if (config.is_encrypted()) {
    // Returning Unsupported forces Chrome to use its own decryption logic. It
    // then creates another NullVideoDecoder with the now unencrypted config.
    std::move(bound_init_cb)
        .Run(DecoderStatus::Codes::kUnsupportedEncryptionMode);
    return;
  }

  // Keep the decoded frame geometry consistent with the demuxer config. The
  // backing allocation is shared by every output frame for this coded size.
  if (!black_frame_ || black_frame_->coded_size() != config.coded_size()) {
    black_frame_ = VideoFrame::CreateBlackFrame(config.coded_size());
    CHECK(black_frame_);
  }

  config_ = config;
  output_cb_ = output_cb;
  initialized_ = true;
  decode_finished_ = false;
  std::move(bound_init_cb).Run(DecoderStatus::Codes::kOk);
}

void NullVideoDecoder::Decode(scoped_refptr<DecoderBuffer> buffer,
                              DecodeCB decode_cb) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  CHECK(initialized_);
  CHECK(buffer);

  DecodeCB bound_decode_cb =
      base::BindPostTaskToCurrentDefault(std::move(decode_cb));
  if (decode_finished_ || buffer->end_of_stream()) {
    decode_finished_ = true;
    std::move(bound_decode_cb).Run(DecoderStatus::Codes::kOk);
    return;
  }

  scoped_refptr<VideoFrame> output = VideoFrame::WrapVideoFrame(
      black_frame_, black_frame_->format(), config_.visible_rect(),
      config_.natural_size());
  CHECK(output);
  output->set_timestamp(buffer->timestamp());
  if (config_.color_space_info().IsSpecified()) {
    output->set_color_space(config_.color_space_info().ToGfxColorSpace());
  }
  output->set_hdr_metadata(config_.hdr_metadata());

  output_cb_.Run(std::move(output));
  std::move(bound_decode_cb).Run(DecoderStatus::Codes::kOk);
}

void NullVideoDecoder::Reset(base::OnceClosure reset_cb) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  decode_finished_ = false;
  base::BindPostTaskToCurrentDefault(std::move(reset_cb)).Run();
}

}  // namespace media

// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef MEDIA_FILTERS_NULL_VIDEO_DECODER_H_
#define MEDIA_FILTERS_NULL_VIDEO_DECODER_H_

#include "base/sequence_checker.h"
#include "media/base/video_decoder.h"
#include "media/base/video_decoder_config.h"
#include "media/base/video_frame.h"

namespace media {

// A lightweight decoder that ignores compressed input and emits black frames
// with the input timestamps. This keeps the media timeline and renderer state
// moving without performing codec decoding.
class MEDIA_EXPORT NullVideoDecoder final : public VideoDecoder {
 public:
  NullVideoDecoder();

  NullVideoDecoder(const NullVideoDecoder&) = delete;
  NullVideoDecoder& operator=(const NullVideoDecoder&) = delete;

  ~NullVideoDecoder() override;

  // VideoDecoder implementation.
  VideoDecoderType GetDecoderType() const override;
  void Initialize(const VideoDecoderConfig& config,
                  bool low_delay,
                  CdmContext* cdm_context,
                  InitCB init_cb,
                  const OutputCB& output_cb,
                  const WaitingCB& waiting_cb) override;
  void Decode(scoped_refptr<DecoderBuffer> buffer, DecodeCB decode_cb) override;
  void Reset(base::OnceClosure reset_cb) override;

 private:
  SEQUENCE_CHECKER(sequence_checker_);

  bool initialized_ = false;
  bool decode_finished_ = false;
  VideoDecoderConfig config_;
  OutputCB output_cb_;
  scoped_refptr<VideoFrame> black_frame_;
};

}  // namespace media

#endif  // MEDIA_FILTERS_NULL_VIDEO_DECODER_H_

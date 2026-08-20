// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef MEDIA_FILTERS_NULL_AUDIO_DECODER_H_
#define MEDIA_FILTERS_NULL_AUDIO_DECODER_H_

#include <memory>
#include <optional>

#include "base/sequence_checker.h"
#include "base/time/time.h"
#include "media/base/audio_decoder.h"
#include "media/base/audio_decoder_config.h"
#include "media/base/audio_discard_helper.h"

namespace media {

// A lightweight decoder that ignores compressed input and emits silent audio
// buffers. The buffers carry the original stream timing but no sample
// allocation, allowing the normal audio clock and renderer state to advance
// without codec work.
class MEDIA_EXPORT NullAudioDecoder final : public AudioDecoder {
 public:
  NullAudioDecoder();

  NullAudioDecoder(const NullAudioDecoder&) = delete;
  NullAudioDecoder& operator=(const NullAudioDecoder&) = delete;

  ~NullAudioDecoder() override;

  // AudioDecoder implementation.
  AudioDecoderType GetDecoderType() const override;
  void Initialize(const AudioDecoderConfig& config,
                  CdmContext* cdm_context,
                  InitCB init_cb,
                  const OutputCB& output_cb,
                  const WaitingCB& waiting_cb) override;
  void Decode(scoped_refptr<DecoderBuffer> buffer, DecodeCB decode_cb) override;
  void Reset(base::OnceClosure reset_cb) override;

 private:
  void EmitSilentBuffer(AudioDiscardHelper::TimeInfo time_info,
                        base::TimeDelta duration);
  base::TimeDelta DurationForFinalBuffer() const;

  SEQUENCE_CHECKER(sequence_checker_);

  bool initialized_ = false;
  bool decode_finished_ = false;
  AudioDecoderConfig config_;
  OutputCB output_cb_;
  std::unique_ptr<AudioDiscardHelper> discard_helper_;
  std::optional<AudioDiscardHelper::TimeInfo> pending_time_info_;
  base::TimeDelta last_duration_;
};

}  // namespace media

#endif  // MEDIA_FILTERS_NULL_AUDIO_DECODER_H_

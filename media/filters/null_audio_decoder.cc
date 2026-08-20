// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "media/filters/null_audio_decoder.h"

#include <algorithm>
#include <limits>
#include <utility>

#include "base/check.h"
#include "base/task/bind_post_task.h"
#include "media/base/audio_buffer.h"
#include "media/base/audio_timestamp_helper.h"
#include "media/base/decoder_buffer.h"
#include "media/base/timestamp_constants.h"

namespace media {

namespace {

// Only the final packet needs an estimate: packets without durations are held
// until the next timestamp can provide an exact value. Twenty milliseconds is
// a common audio packet duration and keeps the clock moving at end of stream.
constexpr base::TimeDelta kFinalBufferFallbackDuration = base::Milliseconds(20);

}  // namespace

NullAudioDecoder::NullAudioDecoder() {
  DETACH_FROM_SEQUENCE(sequence_checker_);
}

NullAudioDecoder::~NullAudioDecoder() {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
}

AudioDecoderType NullAudioDecoder::GetDecoderType() const {
  return AudioDecoderType::kNull;
}

void NullAudioDecoder::Initialize(const AudioDecoderConfig& config,
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
  discard_helper_.reset();
  pending_time_info_.reset();
  last_duration_ = base::TimeDelta();

  if (config.is_encrypted()) {
    // Let Chromium's decrypting path produce clear buffers, after which it can
    // initialize another NullAudioDecoder with the clear configuration.
    std::move(bound_init_cb)
        .Run(DecoderStatus::Codes::kUnsupportedEncryptionMode);
    return;
  }

  config_ = config;
  output_cb_ = base::BindPostTaskToCurrentDefault(output_cb);
  discard_helper_ = std::make_unique<AudioDiscardHelper>(
      config.samples_per_second(), /*decoder_delay=*/0,
      /*delayed_discard=*/false);
  discard_helper_->Reset(/*initial_discard=*/0);
  initialized_ = true;
  decode_finished_ = false;
  std::move(bound_init_cb).Run(DecoderStatus::Codes::kOk);
}

void NullAudioDecoder::Decode(scoped_refptr<DecoderBuffer> buffer,
                              DecodeCB decode_cb) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  CHECK(initialized_);
  CHECK(buffer);

  DecodeCB bound_decode_cb =
      base::BindPostTaskToCurrentDefault(std::move(decode_cb));
  if (decode_finished_) {
    std::move(bound_decode_cb).Run(DecoderStatus::Codes::kOk);
    return;
  }

  if (buffer->end_of_stream()) {
    if (pending_time_info_) {
      EmitSilentBuffer(*pending_time_info_, DurationForFinalBuffer());
      pending_time_info_.reset();
    }
    decode_finished_ = true;
    std::move(bound_decode_cb).Run(DecoderStatus::Codes::kOk);
    return;
  }

  if (buffer->is_encrypted()) {
    std::move(bound_decode_cb)
        .Run(DecoderStatus::Codes::kUnsupportedEncryptionMode);
    return;
  }

  if (buffer->timestamp() == kNoTimestamp) {
    std::move(bound_decode_cb).Run(DecoderStatus::Codes::kFailed);
    return;
  }

  const AudioDiscardHelper::TimeInfo time_info =
      AudioDiscardHelper::TimeInfo::FromBuffer(*buffer);

  if (pending_time_info_) {
    const base::TimeDelta inferred_duration =
        time_info.timestamp - pending_time_info_->timestamp;
    const base::TimeDelta pending_duration = inferred_duration.is_positive()
                                                 ? inferred_duration
                                                 : DurationForFinalBuffer();
    EmitSilentBuffer(*pending_time_info_, pending_duration);
    last_duration_ = pending_duration;
    pending_time_info_.reset();
  }

  if (time_info.duration.is_positive()) {
    EmitSilentBuffer(time_info, time_info.duration);
    last_duration_ = time_info.duration;
  } else {
    // FFmpeg demuxing commonly leaves packet duration unknown. Retain just the
    // timing metadata, not the compressed payload, and infer it from the next
    // packet timestamp.
    pending_time_info_ = time_info;
  }

  std::move(bound_decode_cb).Run(DecoderStatus::Codes::kOk);
}

void NullAudioDecoder::Reset(base::OnceClosure reset_cb) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  decode_finished_ = false;
  pending_time_info_.reset();
  last_duration_ = base::TimeDelta();
  discard_helper_->Reset(/*initial_discard=*/0);
  base::BindPostTaskToCurrentDefault(std::move(reset_cb)).Run();
}

void NullAudioDecoder::EmitSilentBuffer(AudioDiscardHelper::TimeInfo time_info,
                                        base::TimeDelta duration) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  CHECK(duration.is_positive());

  time_info.duration = duration;
  const int64_t frames = AudioTimestampHelper::TimeToFrames(
      duration, config_.samples_per_second());
  const int frame_count = static_cast<int>(
      std::clamp<int64_t>(frames, 1, std::numeric_limits<int>::max()));
  scoped_refptr<AudioBuffer> output = AudioBuffer::CreateEmptyBuffer(
      config_.channel_layout(), config_.channels(),
      config_.samples_per_second(), frame_count, time_info.timestamp);

  if (discard_helper_->ProcessBuffers(time_info, output.get())) {
    output_cb_.Run(std::move(output));
  }
}

base::TimeDelta NullAudioDecoder::DurationForFinalBuffer() const {
  return last_duration_.is_positive() ? last_duration_
                                      : kFinalBufferFallbackDuration;
}

}  // namespace media

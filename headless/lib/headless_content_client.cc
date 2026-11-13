// Copyright 2015 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "headless/lib/headless_content_client.h"

#include <utility>

#include "base/memory/ref_counted_memory.h"
#include "build/build_config.h"
#include "components/embedder_support/origin_trials/origin_trial_policy_impl.h"
#include "media/media_buildflags.h"
#include "third_party/widevine/cdm/buildflags.h"
#include "ui/base/l10n/l10n_util.h"
#include "ui/base/resource/resource_bundle.h"

#if BUILDFLAG(IS_LINUX) && BUILDFLAG(ENABLE_WIDEVINE) && \
    BUILDFLAG(ENABLE_LIBRARY_CDMS)
#include "base/command_line.h"
#include "base/files/file_util.h"
#include "base/logging.h"
#include "base/native_library.h"
#include "components/cdm/common/cdm_manifest.h"
#include "content/public/common/cdm_info.h"
#include "headless/public/switches.h"
#include "media/base/cdm_capability.h"
#include "media/cdm/cdm_paths.h"
#include "third_party/widevine/cdm/widevine_cdm_common.h"
#endif

namespace headless {

HeadlessContentClient::HeadlessContentClient() = default;

HeadlessContentClient::~HeadlessContentClient() = default;

void HeadlessContentClient::AddContentDecryptionModules(
    std::vector<content::CdmInfo>* cdms,
    std::vector<media::CdmHostFilePath>* cdm_host_file_paths) {
#if BUILDFLAG(IS_LINUX) && BUILDFLAG(ENABLE_WIDEVINE) && \
    BUILDFLAG(ENABLE_LIBRARY_CDMS)
  if (!cdms) {
    return;
  }
  const auto directory =
      base::CommandLine::ForCurrentProcess()->GetSwitchValuePath(
          switches::kWidevineCdmDirectory);
  if (directory.empty()) {
    return;
  }
  // Both the browser and the pre-sandbox zygote call this hook. Use the same
  // explicitly provisioned CDM in each; do not discover or update it at
  // runtime.
  if (!directory.IsAbsolute()) {
    LOG(ERROR) << "--widevine-cdm-directory requires an absolute path";
    return;
  }
  const auto library = media::GetPlatformSpecificDirectory(directory).Append(
      base::GetNativeLibraryName(kWidevineCdmLibraryName));
  media::CdmCapability capability;
  if (!base::PathExists(library) ||
      !ParseCdmManifestFromPath(
          directory.Append(FILE_PATH_LITERAL("manifest.json")), &capability)) {
    LOG(ERROR) << "Missing or incompatible Widevine CDM in " << directory;
    return;
  }
  // This mode supports temporary sessions only, including incognito contexts.
  capability.session_types = {media::CdmSessionType::kTemporary};
  cdms->emplace_back(kWidevineKeySystem,
                     content::CdmInfo::Robustness::kSoftwareSecure,
                     std::move(capability), /*supports_sub_key_systems=*/false,
                     kWidevineCdmDisplayName, kWidevineCdmType, library);
#endif
}

std::u16string HeadlessContentClient::GetLocalizedString(int message_id) {
  return l10n_util::GetStringUTF16(message_id);
}

std::string_view HeadlessContentClient::GetDataResource(
    int resource_id,
    ui::ResourceScaleFactor scale_factor) {
  return ui::ResourceBundle::GetSharedInstance().GetRawDataResourceForScale(
      resource_id, scale_factor);
}

scoped_refptr<base::RefCountedMemory>
HeadlessContentClient::GetDataResourceBytes(int resource_id) {
  return ui::ResourceBundle::GetSharedInstance().LoadDataResourceBytes(
      resource_id);
}

std::string HeadlessContentClient::GetDataResourceString(int resource_id) {
  return ui::ResourceBundle::GetSharedInstance().LoadDataResourceString(
      resource_id);
}

gfx::Image& HeadlessContentClient::GetNativeImageNamed(int resource_id) {
  return ui::ResourceBundle::GetSharedInstance().GetNativeImageNamed(
      resource_id);
}

blink::OriginTrialPolicy* HeadlessContentClient::GetOriginTrialPolicy() {
  // Prevent initialization race (see crbug.com/721144). There may be a
  // race when the policy is needed for worker startup (which happens on a
  // separate worker thread).
  base::AutoLock auto_lock(origin_trial_policy_lock_);
  if (!origin_trial_policy_)
    origin_trial_policy_ =
        std::make_unique<embedder_support::OriginTrialPolicyImpl>();
  return origin_trial_policy_.get();
}

}  // namespace headless

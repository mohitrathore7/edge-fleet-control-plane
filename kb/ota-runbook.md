# OTA failure runbook

## E_DOWNLOAD_TIMEOUT
Image download aborted after repeated chunk retries. Typical cause: lossy cellular/Wi-Fi link or
long network partitions. Remediation: check link quality telemetry for the device, confirm the
artifact CDN is reachable, and let the reconciler retry once the link is stable. Downloads are
resumable, so retries do not restart from zero.

## E_CHECKSUM_MISMATCH
The downloaded image's sha256 did not match the rollout manifest; the image was rejected before
install. Cause: wrong sha256 in the rollout spec, corrupted artifact in storage, or a tampered
download. Remediation: verify the artifact digest in the registry, re-publish the artifact, and
recreate the rollout. Never bypass verification.

## E_NO_SPACE
The inactive A/B slot could not be written because /data has too little free space. Remediation:
clean old model caches and logs on the device, or exclude low-storage hardware from the rollout
with target_hw_class.

## E_HEALTHCHECK_FAILED / RUNTIME_CRASH / OTA_ROLLBACK
The new image booted but the post-update health check failed (for example the inference runtime
crashed with SIGSEGV while loading the model), so the bootloader reverted to the previous slot.
If the crash is limited to specific hw_class values (e.g. NPU delegate libraries), the build is
incompatible with that hardware. Remediation: halt the rollout, reproduce on the affected board,
fix the delegate/driver, and add the board to the CI hardware test matrix.

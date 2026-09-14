#!/sbin/openrc-run
# S10-T2 Alpine Linux boot unit for the USB gateway (P1: Linux Boot).
# Installed into /etc/init.d/mercure-gateway on the P1 ext4 partition;
# enabled with `rc-update add mercure-gateway default` during image build.
#
# Alpine uses OpenRC (not systemd) — this is the P1-mode counterpart of
# systemd/mercure-gateway.service. Validation deferred to the flashed
# hardware leg (docs/qa/usb-uat-10.md §2); syntax is POSIX-sh, verified with
# `sh -n` in CI-adjacent checks.

description="QuantumRAD / mercure DICOM gateway (USB dongle Linux mode)"

# P3 (shared exFAT data partition) is mounted at /mnt/usbdata by
# /etc/fstab in the image; the config points the spool there so the data
# survives mode switches (usb-dongle-spec §4.2).
command="/opt/mercure-gateway/bin/mercure-gateway"
command_args="--config /mnt/usbdata/mercure-gateway.json --web"
command_background="yes"
pidfile="/run/${SVCNAME}.pid"

depend() {
    need net localmount
    after bootmisc
}

start_pre() {
    checkpath --directory --owner root:root --mode 0755 \
        /mnt/usbdata/spool /mnt/usbdata/logs
}

stop_post() {
    # §7.2 graceful sequence already ran inside the gateway (receiver.stop →
    # flush → fsync → shutdown marker). Nothing extra here beyond OpenRC's
    # SIGTERM; 10 s K10 flush bound is enforced gateway-side, so give the
    # supervisor a little slack on top.
    return 0
}

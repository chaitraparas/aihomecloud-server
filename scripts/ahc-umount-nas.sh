#!/bin/sh
# Runs OUTSIDE the aihomecloud sandbox (host mount namespace).
# Bare `umount`, not /usr/bin/umount — see ahc-mount-nas.sh for why (no usr-merge on some boards).
umount "$1" || umount -l "$1"

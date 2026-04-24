#!/bin/bash
# Launched as PID 1 in Modal sandboxes that opt into Docker-in-Docker via
# `image.enable_docker: true`. Configures iptables-legacy (required by the
# gVisor-based sandbox), sets up NAT for container egress, then execs dockerd.
#
# Based on the published Modal recipe:
#   https://modal.com/docs/guide/docker-in-sandboxes

set -xe -o pipefail

dev=$(ip route show default | awk '/default/ {print $5}')
addr=$(ip addr show dev "$dev" | grep -w inet | awk '{print $2}' | cut -d/ -f1)

echo 1 > /proc/sys/net/ipv4/ip_forward
iptables-legacy -t nat -A POSTROUTING -o "$dev" -j SNAT --to-source "$addr" -p tcp
iptables-legacy -t nat -A POSTROUTING -o "$dev" -j SNAT --to-source "$addr" -p udp

update-alternatives --set iptables  /usr/sbin/iptables-legacy
update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy

exec /usr/bin/dockerd --iptables=false --ip6tables=false --log-level=warn

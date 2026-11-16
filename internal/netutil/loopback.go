package netutil

import (
	"fmt"
	"net"
	"strconv"
	"strings"
)

func EnsureLoopbackAddress(addr string) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("address %q must be host:port: %w", addr, err)
	}
	host = strings.Trim(host, "[]")
	if host == "" {
		return fmt.Errorf("empty host is not allowed; use an explicit loopback address")
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return fmt.Errorf("address %q is not a literal loopback IP", addr)
	}
	return nil
}

func EnsureLoopbackListener(addr string) (net.Addr, error) {
	if err := EnsureLoopbackAddress(addr); err != nil {
		return nil, err
	}
	udpAddr, err := net.ResolveUDPAddr("udp", addr)
	if err != nil {
		return nil, err
	}
	if !udpAddr.IP.IsLoopback() {
		return nil, fmt.Errorf("resolved address %s is not loopback", udpAddr)
	}
	return udpAddr, nil
}

func PortOf(addr net.Addr) int {
	if addr == nil {
		return 0
	}
	_, port, err := net.SplitHostPort(addr.String())
	if err != nil {
		return 0
	}
	n, _ := strconv.Atoi(port)
	return n
}

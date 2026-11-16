package main

import (
	"encoding/binary"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"localdns/internal/dnswire"
	"localdns/internal/model"
)

func main() {
	outDir := flag.String("out", "samples", "output directory")
	flag.Parse()
	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		fail(err)
	}
	query := dnswire.Message{
		Header:    dnswire.Header{ID: 0x1234, Flags: dnswire.FlagRD},
		Questions: []dnswire.Question{{Name: "same.demo.test", Type: model.TypeA, Class: model.ClassIN}},
	}
	queryWire, err := dnswire.EncodeMessage(query)
	if err != nil {
		fail(err)
	}
	if err := os.WriteFile(filepath.Join(*outDir, "query.wire"), queryWire, 0o644); err != nil {
		fail(err)
	}

	// The answer uses a compression pointer at the answer owner. This is a
	// two-byte-length-prefixed DNS-over-TCP frame, exercising both the TCP
	// framing and pointer-expansion paths used by the sample loader.
	answer := make([]byte, 12)
	binary.BigEndian.PutUint16(answer[0:2], 0x5678)
	binary.BigEndian.PutUint16(answer[2:4], dnswire.FlagQR|dnswire.FlagAA|dnswire.FlagRA|dnswire.FlagRD)
	binary.BigEndian.PutUint16(answer[4:6], 1)
	binary.BigEndian.PutUint16(answer[6:8], 1)
	questionName := encodeName("same.demo.test")
	answer = append(answer, questionName...)
	answer = append(answer, 0, 1, 0, 1)  // A, IN
	answer = append(answer, 0xc0, 0x0c)  // compressed owner -> question name at offset 12
	answer = append(answer, 0, 1, 0, 1)  // A, IN
	answer = append(answer, 0, 0, 0, 45) // TTL
	answer = append(answer, 0, 4)        // RDLENGTH
	answer = append(answer, 192, 0, 2, 77)
	framed := make([]byte, 2, len(answer)+2)
	binary.BigEndian.PutUint16(framed, uint16(len(answer)))
	framed = append(framed, answer...)
	if err := os.WriteFile(filepath.Join(*outDir, "answer.wire"), framed, 0o644); err != nil {
		fail(err)
	}
	fmt.Printf("wrote %s and %s\n", filepath.Join(*outDir, "query.wire"), filepath.Join(*outDir, "answer.wire"))
}

func encodeName(name string) []byte {
	out := make([]byte, 0, len(name)+2)
	for _, part := range splitName(name) {
		out = append(out, byte(len(part)))
		out = append(out, part...)
	}
	return append(out, 0)
}

func splitName(name string) []string {
	var out []string
	start := 0
	for i := 0; i < len(name); i++ {
		if name[i] == '.' {
			out = append(out, name[start:i])
			start = i + 1
		}
	}
	if start < len(name) {
		out = append(out, name[start:])
	}
	return out
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "mkfixtures:", err)
	os.Exit(1)
}

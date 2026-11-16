package dnswire

import (
	"bytes"
	"encoding/binary"
	"strings"
	"testing"

	"localdns/internal/biz"
	"localdns/internal/model"
)

func TestParseCompressionPointerDefenses(t *testing.T) {
	tests := []struct {
		name       string
		pointer    [2]byte
		wantOffset int
		wantDetail string
	}{
		{name: "self loop", pointer: [2]byte{0xc0, 0x0c}, wantOffset: 12, wantDetail: "forward"},
		{name: "forward", pointer: [2]byte{0xc0, 0x10}, wantOffset: 12, wantDetail: "forward"},
		{name: "out of bounds", pointer: [2]byte{0xc3, 0xe8}, wantOffset: 12, wantDetail: "out of bounds"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			payload := make([]byte, 12)
			binary.BigEndian.PutUint16(payload[4:6], 1)
			payload = append(payload, tc.pointer[0], tc.pointer[1], 0, 1, 0, 1)
			_, err := ParseMessage(payload)
			if err == nil {
				t.Fatal("expected E_WIRE")
			}
			if err.Code != biz.CodeWire || !err.HasOffset || err.Offset != tc.wantOffset {
				t.Fatalf("unexpected error: %+v", err)
			}
			if !strings.Contains(err.Detail, tc.wantDetail) {
				t.Fatalf("detail %q does not contain %q", err.Detail, tc.wantDetail)
			}
		})
	}
}

func TestWireRoundTripSupportedTypes(t *testing.T) {
	msg := Message{
		Header:    Header{ID: 0x1234, Flags: FlagQR | FlagAA | FlagRD | FlagRA},
		Questions: []Question{{Name: "WWW.DEMO.TEST.", Type: model.TypeA, Class: model.ClassIN}},
		Answers: []RR{
			{Name: "www.demo.test", Type: model.TypeA, Class: uint16(model.ClassIN), TTL: 10, Data: "192.0.2.10"},
			{Name: "www.demo.test", Type: model.TypeAAAA, Class: uint16(model.ClassIN), TTL: 20, Data: "2001:db8::10"},
			{Name: "alias.demo.test", Type: model.TypeCNAME, Class: uint16(model.ClassIN), TTL: 30, Data: "WWW.DEMO.TEST."},
			{Name: "demo.test", Type: model.TypeNS, Class: uint16(model.ClassIN), TTL: 40, Data: "ns1.demo.test."},
			{Name: "demo.test", Type: model.TypeSOA, Class: uint16(model.ClassIN), TTL: 50, Data: "ns1.demo.test hostmaster.demo.test 2026111701 3600 600 86400 30"},
		},
	}
	wire, appErr := EncodeMessage(msg)
	if appErr != nil {
		t.Fatal(appErr)
	}
	parsed, appErr := ParseMessage(wire)
	if appErr != nil {
		t.Fatal(appErr)
	}
	if parsed.Header.ID != msg.Header.ID || parsed.Header.Flags != msg.Header.Flags {
		t.Fatalf("header mismatch: got %+v want %+v", parsed.Header, msg.Header)
	}
	if len(parsed.Answers) != len(msg.Answers) {
		t.Fatalf("answer count %d want %d", len(parsed.Answers), len(msg.Answers))
	}
	if parsed.Questions[0].Name != "www.demo.test" {
		t.Fatalf("question name not case-folded: %s", parsed.Questions[0].Name)
	}
	if parsed.Answers[2].Data != "www.demo.test" {
		t.Fatalf("CNAME target not normalized: %s", parsed.Answers[2].Data)
	}
	for i := range msg.Answers {
		if parsed.Answers[i].TTL != msg.Answers[i].TTL {
			t.Fatalf("TTL mismatch at %d", i)
		}
	}
}

func TestTCPFrameRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	if err := WriteTCPFrame(&buf, []byte("dns")); err != nil {
		t.Fatal(err)
	}
	got, err := ReadTCPFrame(&buf, 65535)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "dns" {
		t.Fatalf("got %q", got)
	}
	if _, err := ReadTCPFrame(bytes.NewReader([]byte{0, 0}), 65535); err != ErrEmptyFrame {
		t.Fatalf("want ErrEmptyFrame, got %v", err)
	}
}

func TestFitUDPTruncation(t *testing.T) {
	msg := Message{
		Header:    Header{ID: 7, Flags: FlagQR | FlagAA},
		Questions: []Question{{Name: "bulk.demo.test", Type: model.TypeA, Class: model.ClassIN}},
	}
	for i := 0; i < 40; i++ {
		msg.Answers = append(msg.Answers, RR{
			Name: "bulk.demo.test", Type: model.TypeA, Class: uint16(model.ClassIN), TTL: 60,
			Data: bytesToIP(192, 0, 2, byte(i)),
		})
	}
	fitted, truncated, appErr := FitUDP(msg, 512)
	if appErr != nil {
		t.Fatal(appErr)
	}
	if !truncated {
		t.Fatal("expected truncation")
	}
	if len(fitted) > 512 {
		t.Fatalf("fitted response is %d bytes", len(fitted))
	}
	parsed, appErr := ParseMessage(fitted)
	if appErr != nil {
		t.Fatal(appErr)
	}
	if parsed.Header.Flags&FlagTC == 0 {
		t.Fatal("TC bit is not set")
	}
	if len(parsed.Answers) != 0 || len(parsed.Questions) != 1 {
		t.Fatalf("unexpected truncated sections: q=%d an=%d", len(parsed.Questions), len(parsed.Answers))
	}
}

func TestMetadataOptionRoundTrip(t *testing.T) {
	payload := []byte(`{"source":"authority"}`)
	rr := MetadataRR(payload)
	msg := Message{Additionals: []RR{rr}}
	got, ok := FindOption(msg, MetadataOptionCode)
	if !ok || string(got) != string(payload) {
		t.Fatalf("option mismatch: %q %v", got, ok)
	}
}

func bytesToIP(a, b, c, d byte) string {
	return string([]byte{}) + itoa(int(a)) + "." + itoa(int(b)) + "." + itoa(int(c)) + "." + itoa(int(d))
}

func itoa(v int) string {
	if v == 0 {
		return "0"
	}
	var digits [3]byte
	n := 0
	for v > 0 {
		digits[n] = byte('0' + v%10)
		v /= 10
		n++
	}
	for i := 0; i < n/2; i++ {
		digits[i], digits[n-1-i] = digits[n-1-i], digits[i]
	}
	return string(digits[:n])
}

package dnswire

import (
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"net"
	"strings"

	"localdns/internal/biz"
	"localdns/internal/model"
)

const (
	FlagQR uint16 = 0x8000
	FlagAA uint16 = 0x0400
	FlagTC uint16 = 0x0200
	FlagRD uint16 = 0x0100
	FlagRA uint16 = 0x0080
)

const MetadataOptionCode uint16 = 65002

type Header struct {
	ID      uint16
	Flags   uint16
	QDCount uint16
	ANCount uint16
	NSCount uint16
	ARCount uint16
}

func (h Header) RCode() int       { return int(h.Flags & 0x000f) }
func (h Header) Opcode() int      { return int((h.Flags >> 11) & 0x000f) }
func (h Header) IsResponse() bool { return h.Flags&FlagQR != 0 }

type Question struct {
	Name  string
	Type  model.Type
	Class model.Class
}

type RR struct {
	Name  string
	Type  model.Type
	Class uint16
	TTL   uint32
	Data  string
	Raw   []byte
}

type Message struct {
	Header      Header
	Questions   []Question
	Answers     []RR
	Authorities []RR
	Additionals []RR
}

func NewQuery(id uint16, name string, t model.Type, class model.Class) (Message, *biz.Error) {
	canonical, err := model.CanonicalName(name)
	if err != nil {
		return Message{}, biz.New(biz.CodeQuery, "invalid query name: "+err.Error())
	}
	return Message{
		Header:    Header{ID: id, Flags: FlagRD},
		Questions: []Question{{Name: canonical, Type: t, Class: class}},
	}, nil
}

func ParseMessage(data []byte) (Message, *biz.Error) {
	if len(data) < 12 {
		return Message{}, biz.At(biz.CodeWire, 0, "message is shorter than the 12-byte header")
	}
	msg := Message{Header: Header{
		ID:      binary.BigEndian.Uint16(data[0:2]),
		Flags:   binary.BigEndian.Uint16(data[2:4]),
		QDCount: binary.BigEndian.Uint16(data[4:6]),
		ANCount: binary.BigEndian.Uint16(data[6:8]),
		NSCount: binary.BigEndian.Uint16(data[8:10]),
		ARCount: binary.BigEndian.Uint16(data[10:12]),
	}}
	off := 12
	for i := 0; i < int(msg.Header.QDCount); i++ {
		name, next, err := parseName(data, off)
		if err != nil {
			return Message{}, err
		}
		if next+4 > len(data) {
			return Message{}, biz.At(biz.CodeWire, next, "truncated question type/class")
		}
		msg.Questions = append(msg.Questions, Question{
			Name:  name,
			Type:  model.Type(binary.BigEndian.Uint16(data[next : next+2])),
			Class: model.Class(binary.BigEndian.Uint16(data[next+2 : next+4])),
		})
		off = next + 4
	}
	var err *biz.Error
	msg.Answers, off, err = parseRRs(data, off, int(msg.Header.ANCount))
	if err != nil {
		return Message{}, err
	}
	msg.Authorities, off, err = parseRRs(data, off, int(msg.Header.NSCount))
	if err != nil {
		return Message{}, err
	}
	msg.Additionals, _, err = parseRRs(data, off, int(msg.Header.ARCount))
	if err != nil {
		return Message{}, err
	}
	return msg, nil
}

func parseRRs(data []byte, off, count int) ([]RR, int, *biz.Error) {
	rrs := make([]RR, 0, count)
	for i := 0; i < count; i++ {
		rr, next, err := parseRR(data, off)
		if err != nil {
			return nil, off, err
		}
		rrs = append(rrs, rr)
		off = next
	}
	return rrs, off, nil
}

func parseRR(data []byte, off int) (RR, int, *biz.Error) {
	name, next, err := parseName(data, off)
	if err != nil {
		return RR{}, off, err
	}
	off = next
	if off+10 > len(data) {
		return RR{}, off, biz.At(biz.CodeWire, off, "truncated resource-record header")
	}
	rr := RR{
		Name:  name,
		Type:  model.Type(binary.BigEndian.Uint16(data[off : off+2])),
		Class: binary.BigEndian.Uint16(data[off+2 : off+4]),
		TTL:   binary.BigEndian.Uint32(data[off+4 : off+8]),
	}
	rdlen := int(binary.BigEndian.Uint16(data[off+8 : off+10]))
	rstart := off + 10
	rend := rstart + rdlen
	if rend > len(data) {
		return RR{}, off, biz.At(biz.CodeWire, off, "RDATA exceeds message bounds")
	}
	switch rr.Type {
	case model.TypeA:
		if rdlen != 4 {
			return RR{}, rstart, biz.At(biz.CodeWire, rstart, "A RDATA must be 4 bytes")
		}
		rr.Data = net.IP(data[rstart:rend]).String()
	case model.TypeAAAA:
		if rdlen != 16 {
			return RR{}, rstart, biz.At(biz.CodeWire, rstart, "AAAA RDATA must be 16 bytes")
		}
		rr.Data = net.IP(data[rstart:rend]).String()
	case model.TypeCNAME, model.TypeNS:
		target, targetNext, nameErr := parseName(data, rstart)
		if nameErr != nil {
			return RR{}, rstart, nameErr
		}
		if targetNext != rend {
			return RR{}, targetNext, biz.At(biz.CodeWire, targetNext, "compressed name does not fill RDATA")
		}
		rr.Data = target
	case model.TypeSOA:
		soa, soaNext, soaErr := parseSOA(data, rstart, rend)
		if soaErr != nil {
			return RR{}, rstart, soaErr
		}
		if soaNext != rend {
			return RR{}, soaNext, biz.At(biz.CodeWire, soaNext, "SOA RDATA has trailing bytes")
		}
		rr.Data = soa
	default:
		rr.Raw = append([]byte(nil), data[rstart:rend]...)
		if len(rr.Raw) > 0 {
			rr.Data = "#" + hex.EncodeToString(rr.Raw)
		}
	}
	return rr, rend, nil
}

func parseSOA(data []byte, off, end int) (string, int, *biz.Error) {
	mname, next, err := parseName(data, off)
	if err != nil {
		return "", off, err
	}
	rname, next, err := parseName(data, next)
	if err != nil {
		return "", next, err
	}
	if next+20 > end {
		return "", next, biz.At(biz.CodeWire, next, "truncated SOA numeric fields")
	}
	nums := make([]uint32, 5)
	for i := range nums {
		nums[i] = binary.BigEndian.Uint32(data[next+i*4 : next+(i+1)*4])
	}
	return fmt.Sprintf("%s %s %d %d %d %d %d", mname, rname, nums[0], nums[1], nums[2], nums[3], nums[4]), next + 20, nil
}

// parseName expands a possibly compressed name. Pointers must point strictly
// backwards; loops, forward pointers, and out-of-bounds targets are rejected.
func parseName(data []byte, start int) (string, int, *biz.Error) {
	if start < 0 || start >= len(data) {
		return "", start, biz.At(biz.CodeWire, start, "name offset is outside the message")
	}
	var labels []string
	pos := start
	next := -1
	seen := make(map[int]bool)
	wireLen := 1
	for {
		if pos < 0 || pos >= len(data) {
			return "", pos, biz.At(biz.CodeWire, pos, "name extends outside the message")
		}
		if seen[pos] {
			return "", pos, biz.At(biz.CodeWire, pos, "compression pointer loop detected")
		}
		seen[pos] = true
		c := data[pos]
		switch c & 0xc0 {
		case 0x00:
			l := int(c)
			if l == 0 {
				if next < 0 {
					next = pos + 1
				}
				if len(labels) == 0 {
					return ".", next, nil
				}
				canonical, err := model.CanonicalName(strings.Join(labels, "."))
				if err != nil {
					return "", pos, biz.At(biz.CodeWire, pos, "invalid name: "+err.Error())
				}
				return canonical, next, nil
			}
			if l > 63 {
				return "", pos, biz.At(biz.CodeWire, pos, "label exceeds 63 bytes")
			}
			if pos+1+l > len(data) {
				return "", pos, biz.At(biz.CodeWire, pos, "label extends outside the message")
			}
			wireLen += 1 + l
			if wireLen > 255 {
				return "", pos, biz.At(biz.CodeWire, pos, "expanded name exceeds 255 bytes")
			}
			labels = append(labels, string(data[pos+1:pos+1+l]))
			pos += 1 + l
		case 0xc0:
			if pos+1 >= len(data) {
				return "", pos, biz.At(biz.CodeWire, pos, "truncated compression pointer")
			}
			target := int(c&0x3f)<<8 | int(data[pos+1])
			if target >= len(data) {
				return "", pos, biz.At(biz.CodeWire, pos, "compression pointer is out of bounds")
			}
			if target >= pos {
				return "", pos, biz.At(biz.CodeWire, pos, "compression pointer points forward or creates a loop")
			}
			if next < 0 {
				next = pos + 2
			}
			pos = target
		default:
			return "", pos, biz.At(biz.CodeWire, pos, "unsupported label type or reserved compression bits")
		}
	}
}

func EncodeMessage(msg Message) ([]byte, *biz.Error) {
	out := make([]byte, 12)
	binary.BigEndian.PutUint16(out[0:2], msg.Header.ID)
	binary.BigEndian.PutUint16(out[2:4], msg.Header.Flags)
	binary.BigEndian.PutUint16(out[4:6], uint16(len(msg.Questions)))
	binary.BigEndian.PutUint16(out[6:8], uint16(len(msg.Answers)))
	binary.BigEndian.PutUint16(out[8:10], uint16(len(msg.Authorities)))
	binary.BigEndian.PutUint16(out[10:12], uint16(len(msg.Additionals)))
	for _, q := range msg.Questions {
		name, err := encodeName(q.Name)
		if err != nil {
			return nil, err
		}
		out = append(out, name...)
		var tail [4]byte
		binary.BigEndian.PutUint16(tail[0:2], uint16(q.Type))
		binary.BigEndian.PutUint16(tail[2:4], uint16(q.Class))
		out = append(out, tail[:]...)
	}
	for _, section := range [][]RR{msg.Answers, msg.Authorities, msg.Additionals} {
		for _, rr := range section {
			encoded, err := encodeRR(rr)
			if err != nil {
				return nil, err
			}
			out = append(out, encoded...)
		}
	}
	if len(out) > 65535 {
		return nil, biz.New(biz.CodeWire, "encoded DNS message exceeds 65535 bytes")
	}
	return out, nil
}

func encodeRR(rr RR) ([]byte, *biz.Error) {
	name, err := encodeName(rr.Name)
	if err != nil {
		return nil, err
	}
	rdata, dataErr := encodeRDATA(rr)
	if dataErr != nil {
		return nil, dataErr
	}
	if len(rdata) > 65535 {
		return nil, biz.New(biz.CodeWire, "RDATA exceeds 65535 bytes")
	}
	out := append([]byte(nil), name...)
	var head [10]byte
	binary.BigEndian.PutUint16(head[0:2], uint16(rr.Type))
	binary.BigEndian.PutUint16(head[2:4], rr.Class)
	binary.BigEndian.PutUint32(head[4:8], rr.TTL)
	binary.BigEndian.PutUint16(head[8:10], uint16(len(rdata)))
	out = append(out, head[:]...)
	out = append(out, rdata...)
	return out, nil
}

func encodeRDATA(rr RR) ([]byte, *biz.Error) {
	switch rr.Type {
	case model.TypeA:
		ip := net.ParseIP(rr.Data)
		if ip == nil || ip.To4() == nil {
			return nil, biz.New(biz.CodeWire, "invalid A RDATA "+rr.Data)
		}
		return append([]byte(nil), ip.To4()...), nil
	case model.TypeAAAA:
		ip := net.ParseIP(rr.Data)
		if ip == nil || ip.To16() == nil || ip.To4() != nil {
			return nil, biz.New(biz.CodeWire, "invalid AAAA RDATA "+rr.Data)
		}
		return append([]byte(nil), ip.To16()...), nil
	case model.TypeCNAME, model.TypeNS:
		return encodeName(rr.Data)
	case model.TypeSOA:
		soa, err := model.ParseSOAData(rr.Data)
		if err != nil {
			return nil, biz.New(biz.CodeWire, "invalid SOA RDATA: "+err.Error())
		}
		mname, encErr := encodeName(soa.MName)
		if encErr != nil {
			return nil, encErr
		}
		rname, encErr := encodeName(soa.RName)
		if encErr != nil {
			return nil, encErr
		}
		out := append(mname, rname...)
		for _, n := range []uint32{soa.Serial, soa.Refresh, soa.Retry, soa.Expire, soa.Minimum} {
			var b [4]byte
			binary.BigEndian.PutUint32(b[:], n)
			out = append(out, b[:]...)
		}
		return out, nil
	default:
		return append([]byte(nil), rr.Raw...), nil
	}
}

func encodeName(name string) ([]byte, *biz.Error) {
	canonical, err := model.CanonicalName(name)
	if err != nil {
		return nil, biz.New(biz.CodeWire, "invalid name "+name+": "+err.Error())
	}
	if canonical == "." {
		return []byte{0}, nil
	}
	out := make([]byte, 0, len(canonical)+2)
	for _, label := range strings.Split(canonical, ".") {
		if len(label) > 63 {
			return nil, biz.New(biz.CodeWire, "label exceeds 63 bytes")
		}
		out = append(out, byte(len(label)))
		out = append(out, label...)
	}
	out = append(out, 0)
	return out, nil
}

// FitUDP returns msg unchanged when it fits; otherwise it returns a valid
// degraded response with TC set and an empty answer/authority/additional set.
func FitUDP(msg Message, limit int) ([]byte, bool, *biz.Error) {
	full, err := EncodeMessage(msg)
	if err != nil {
		return nil, false, err
	}
	if limit < 12 {
		limit = 12
	}
	if len(full) <= limit {
		return full, false, nil
	}
	truncated := msg
	truncated.Header.Flags |= FlagTC
	truncated.Answers = nil
	truncated.Authorities = nil
	truncated.Additionals = nil
	short, err := EncodeMessage(truncated)
	if err != nil {
		return nil, false, err
	}
	if len(short) <= limit {
		return short, true, nil
	}
	truncated.Questions = nil
	short, err = EncodeMessage(truncated)
	if err != nil {
		return nil, false, err
	}
	if len(short) > limit {
		return nil, false, biz.New(biz.CodeWire, "cannot fit a DNS header within UDP limit")
	}
	return short, true, nil
}

func EncodeOption(code uint16, payload []byte) []byte {
	out := make([]byte, 4, 4+len(payload))
	binary.BigEndian.PutUint16(out[0:2], code)
	binary.BigEndian.PutUint16(out[2:4], uint16(len(payload)))
	return append(out, payload...)
}

func MetadataRR(payload []byte) RR {
	return RR{Name: ".", Type: model.TypeOPT, Class: 1232, TTL: 0, Raw: EncodeOption(MetadataOptionCode, payload)}
}

func FindOption(msg Message, code uint16) ([]byte, bool) {
	for _, rr := range msg.Additionals {
		if rr.Type != model.TypeOPT {
			continue
		}
		raw := rr.Raw
		for len(raw) >= 4 {
			optCode := binary.BigEndian.Uint16(raw[0:2])
			optLen := int(binary.BigEndian.Uint16(raw[2:4]))
			raw = raw[4:]
			if optLen > len(raw) {
				return nil, false
			}
			if optCode == code {
				return append([]byte(nil), raw[:optLen]...), true
			}
			raw = raw[optLen:]
		}
	}
	return nil, false
}

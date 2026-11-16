package zone

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"localdns/internal/biz"
	"localdns/internal/model"
)

const sampleDir = "../../samples"

func loadSampleZone(t *testing.T) *Zone {
	t.Helper()
	z, err := LoadJSON(filepath.Join(sampleDir, "zone.json"), Options{})
	if err != nil {
		t.Fatal(err)
	}
	return z
}

func TestDualChannelNormalizationAndOrigins(t *testing.T) {
	z := loadSampleZone(t)
	before, origins, ok := z.ExactRRSet("same.demo.test", model.TypeA)
	if !ok || len(before) != 1 {
		t.Fatal("JSON sample record missing")
	}
	if strings.Join(origins, "+") != OriginJSON {
		t.Fatalf("unexpected pre-merge origins: %v", origins)
	}
	_, wireRecords, err := LoadWireMessage(filepath.Join(sampleDir, "answer.wire"))
	if err != nil {
		t.Fatal(err)
	}
	if len(wireRecords) != 1 {
		t.Fatalf("wire answer count %d", len(wireRecords))
	}
	want, normErr := model.NormalizeRecord("SAME.demo.test.", model.TypeA, model.ClassIN, 45, "192.0.2.77")
	if normErr != nil {
		t.Fatal(normErr)
	}
	if !sameLogicalRecord(want, wireRecords[0]) {
		t.Fatalf("wire normalization mismatch: got %+v want %+v", wireRecords[0], want)
	}
	if err := z.AddWireRecords(wireRecords, OriginWireSample); err != nil {
		t.Fatal(err)
	}
	after, origins, ok := z.ExactRRSet("same.demo.test", model.TypeA)
	if !ok || len(after) != 1 {
		t.Fatal("merged sample record missing")
	}
	if after[0].Data != "192.0.2.77" || after[0].TTL != 45 {
		t.Fatalf("unexpected merged record: %+v", after[0])
	}
	if strings.Join(origins, "+") != OriginJSON+"+"+OriginWireSample {
		t.Fatalf("unexpected merged origins: %v", origins)
	}
	res, err := z.Resolve(model.Question{Name: "same.demo.test", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Answers) != 1 || res.Answers[0].Origin != OriginJSON+"+"+OriginWireSample {
		t.Fatalf("answer did not carry dual origin: %+v", res.Answers)
	}
}

func TestInvalidJSONIsRejectedAtomically(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"fractional ttl", `{"apex":"demo.test","records":[{"name":"x.demo.test","type":"A","ttl":1.5,"rdata":"192.0.2.1"}]}`},
		{"ttl too large", `{"apex":"demo.test","records":[{"name":"x.demo.test","type":"A","ttl":700000,"rdata":"192.0.2.1"}]}`},
		{"unsupported type", `{"apex":"demo.test","records":[{"name":"x.demo.test","type":"TXT","ttl":30,"rdata":"x"}]}`},
		{"bad name", `{"apex":"demo.test","records":[{"name":"bad..name","type":"A","ttl":30,"rdata":"192.0.2.1"}]}`},
		{"bad ip", `{"apex":"demo.test","records":[{"name":"x.demo.test","type":"A","ttl":30,"rdata":"not-an-ip"}]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "zone.json")
			if err := os.WriteFile(path, []byte(tc.body), 0o600); err != nil {
				t.Fatal(err)
			}
			z, err := LoadJSON(path, Options{})
			if err == nil || z != nil {
				t.Fatalf("expected whole-file rejection, got zone=%v err=%v", z, err)
			}
			if biz.CodeOf(err) != biz.CodeZone {
				t.Fatalf("want E_ZONE, got %v", err)
			}
		})
	}
}

func TestWildcardAndExactPrecedence(t *testing.T) {
	z := loadSampleZone(t)
	res, err := z.Resolve(model.Question{Name: "foo.wild.demo.test", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Answers) != 1 || res.Answers[0].Record.Name != "foo.wild.demo.test" || res.Answers[0].Match != model.MatchWildcard {
		t.Fatalf("wildcard answer mismatch: %+v", res.Answers)
	}
	if res.Answers[0].Record.Data != "192.0.2.20" {
		t.Fatalf("wildcard data mismatch: %s", res.Answers[0].Record.Data)
	}
	exactNodeA, err := z.Resolve(model.Question{Name: "wild.demo.test", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if !exactNodeA.Negative || exactNodeA.RCode != model.RCodeNoError || len(exactNodeA.Answers) != 0 {
		t.Fatalf("exact owner with AAAA should produce NODATA, got %+v", exactNodeA)
	}
	exactAAAA, err := z.Resolve(model.Question{Name: "wild.demo.test", Type: model.TypeAAAA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if len(exactAAAA.Answers) != 1 || exactAAAA.Answers[0].Match != model.MatchExact {
		t.Fatalf("exact AAAA mismatch: %+v", exactAAAA.Answers)
	}
	deep, err := z.Resolve(model.Question{Name: "deep.foo.wild.demo.test", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if !deep.Negative || deep.RCode != model.RCodeNXDomain || len(deep.Answers) != 0 {
		t.Fatalf("wildcard must only match one leftmost label: %+v", deep)
	}
}

func TestCaseFoldingCNAMEAndLoop(t *testing.T) {
	z := loadSampleZone(t)
	res, err := z.Resolve(model.Question{Name: "ALIAS.DEMO.TEST.", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Answers) != 2 {
		t.Fatalf("CNAME chain length %d", len(res.Answers))
	}
	if res.Answers[0].Record.Type != model.TypeCNAME || res.Answers[1].Record.Type != model.TypeA {
		t.Fatalf("unexpected CNAME chain: %+v", res.Answers)
	}
	if strings.Join(res.CNAMEChain, ",") != "alias.demo.test -> www.demo.test" {
		t.Fatalf("unexpected chain summary: %v", res.CNAMEChain)
	}
	cnameOnly, err := z.Resolve(model.Question{Name: "alias.demo.test", Type: model.TypeCNAME, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if len(cnameOnly.Answers) != 1 || cnameOnly.Answers[0].Record.Type != model.TypeCNAME {
		t.Fatalf("CNAME qtype should not follow: %+v", cnameOnly.Answers)
	}
	_, err = z.Resolve(model.Question{Name: "loop-a.demo.test", Type: model.TypeA, Class: model.ClassIN})
	if biz.CodeOf(err) != biz.CodeCNAME {
		t.Fatalf("want E_CNAME, got %v", err)
	}
}

func TestNegativeTTLUsesSOAMinimum(t *testing.T) {
	z := loadSampleZone(t)
	res, err := z.Resolve(model.Question{Name: "missing.demo.test", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Negative || res.RCode != model.RCodeNXDomain || res.NegativeTTL != 30 {
		t.Fatalf("unexpected negative result: %+v", res)
	}
	if len(res.Authorities) != 1 || res.Authorities[0].Record.TTL != 30 {
		t.Fatalf("negative SOA TTL not adjusted: %+v", res.Authorities)
	}
}

func TestCNAMEHopLimitAndExternalTarget(t *testing.T) {
	body := `{"apex":"demo.test","records":[
      {"name":"demo.test","type":"SOA","ttl":300,"rdata":"ns1.demo.test hostmaster.demo.test 1 3600 600 86400 30"},
      {"name":"c1.demo.test","type":"CNAME","ttl":30,"rdata":"c2.demo.test"},
      {"name":"c2.demo.test","type":"CNAME","ttl":30,"rdata":"c3.demo.test"},
      {"name":"c3.demo.test","type":"CNAME","ttl":30,"rdata":"c4.demo.test"},
      {"name":"c4.demo.test","type":"A","ttl":30,"rdata":"192.0.2.4"},
      {"name":"ext.demo.test","type":"CNAME","ttl":30,"rdata":"outside.external.test"}
    ]}`
	path := filepath.Join(t.TempDir(), "zone.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	z, err := LoadJSON(path, Options{MaxCNAMEHops: 2})
	if err != nil {
		t.Fatal(err)
	}
	_, err = z.Resolve(model.Question{Name: "c1.demo.test", Type: model.TypeA, Class: model.ClassIN})
	if biz.CodeOf(err) != biz.CodeCNAME {
		t.Fatalf("want E_CNAME for hop limit, got %v", err)
	}
	res, err := z.Resolve(model.Question{Name: "ext.demo.test", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if res.NeedForward != "outside.external.test" || len(res.Answers) != 1 {
		t.Fatalf("external CNAME target not exposed: %+v", res)
	}
}

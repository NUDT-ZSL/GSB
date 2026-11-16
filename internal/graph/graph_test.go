package graph

import (
	"strings"
	"testing"

	"locklease/internal/model"
)

func sampleFile() model.GraphFile {
	return model.GraphFile{Version: 1, Nodes: []model.Node{
		{Key: "A"},
		{Key: "B", DependsOn: []string{"A"}},
		{Key: "C", DependsOn: []string{"A"}},
		{Key: "D", DependsOn: []string{"B", "C"}},
		{Key: "E", DependsOn: []string{"D"}},
	}}
}

func TestBuildAndClosure(t *testing.T) {
	g, err := Build(sampleFile())
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	got := g.DownstreamClosure("A")
	// BFS: level 1 B,C (sorted), level 2 D, level 3 E.
	want := []string{"B", "C", "D", "E"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("closure order = %v, want %v", got, want)
	}
}

func TestCycleReported(t *testing.T) {
	file := model.GraphFile{Nodes: []model.Node{
		{Key: "alpha", DependsOn: []string{"gamma"}},
		{Key: "beta", DependsOn: []string{"alpha"}},
		{Key: "gamma", DependsOn: []string{"beta"}},
	}}
	_, err := Build(file)
	if err == nil {
		t.Fatal("expected E_DEPEND cycle error")
	}
	de, ok := err.(*DependError)
	if !ok || len(de.Cycle) < 3 {
		t.Fatalf("unexpected error: %v", err)
	}
	wantCycle := []string{"alpha", "beta", "gamma", "alpha"}
	if strings.Join(de.Cycle, ",") != strings.Join(wantCycle, ",") {
		t.Fatalf("cycle path = %v, want %v", de.Cycle, wantCycle)
	}
}

func TestMissingReference(t *testing.T) {
	file := model.GraphFile{Nodes: []model.Node{
		{Key: "A"},
		{Key: "B", DependsOn: []string{"ghost"}},
	}}
	_, err := Build(file)
	if err == nil || !strings.Contains(err.Error(), "E_DEPEND") {
		t.Fatalf("expected E_DEPEND, got %v", err)
	}
}

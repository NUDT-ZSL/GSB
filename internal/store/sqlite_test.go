package store

import (
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"configledger/internal/domain"
	"configledger/internal/engine"
	"configledger/internal/spec"
)

func sampleState(t *testing.T, previous *domain.State, name, manifestVersion string, capacity int64) *domain.State {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "samples", name))
	if err != nil {
		t.Fatal(err)
	}
	manifest, graph, berr := spec.Parse(data, name, spec.LoadOptions{AllowDanglingRefs: true})
	if berr != nil {
		t.Fatalf("parse sample: %v", berr)
	}
	if manifestVersion != "" {
		manifest.ManifestVersion = manifestVersion
	}
	state, berr := engine.Apply(previous, manifest, graph, engine.Options{SessionID: "store-test", Capacity: capacity, StorageUnitPrice: 2, CompileUnitPrice: 5})
	if berr != nil {
		t.Fatalf("apply sample: %v", berr)
	}
	return state
}

func TestSnapshotPersistsAcrossReopenAndReplacesActiveVersion(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ledger.db")
	database, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	v1 := sampleState(t, nil, "manifest-v1.json", "", 1200)
	v1Meta, err := database.Save(v1)
	if err != nil {
		t.Fatal(err)
	}
	v2 := sampleState(t, v1, "manifest-v2.json", "", 1200)
	v2Meta, err := database.Save(v2)
	if err != nil {
		t.Fatal(err)
	}
	if err := database.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	restored, activeMeta, err := reopened.LoadActive("store-test")
	if err != nil {
		t.Fatal(err)
	}
	if activeMeta.ID != v2Meta.ID || !reflect.DeepEqual(restored, v2) {
		t.Fatal("active snapshot did not survive database reopen")
	}
	previous, previousMeta, err := reopened.Previous(activeMeta)
	if err != nil {
		t.Fatal(err)
	}
	if previousMeta.ID != v1Meta.ID || !reflect.DeepEqual(previous, v1) {
		t.Fatal("previous snapshot linkage is incorrect")
	}
	if berr := engine.Replay(restored, previous); berr != nil {
		t.Fatalf("replay after reopen: %v", berr)
	}

	if err := reopened.SetActive(v1Meta); err != nil {
		t.Fatal(err)
	}
	restoredV1, currentMeta, err := reopened.LoadActive("store-test")
	if err != nil {
		t.Fatal(err)
	}
	if currentMeta.ID != v1Meta.ID || !reflect.DeepEqual(restoredV1, v1) {
		t.Fatal("active pointer was not replaced by the target snapshot")
	}
	v1Again := sampleState(t, restoredV1, "manifest-v1.json", "", 1200)
	v1AgainMeta, err := reopened.Save(v1Again)
	if err != nil {
		t.Fatal(err)
	}
	if v1AgainMeta.ParentID != v1Meta.ID {
		t.Fatalf("post-restore snapshot parent = %d, want restored snapshot %d", v1AgainMeta.ParentID, v1Meta.ID)
	}
	parent, parentMeta, err := reopened.Previous(v1AgainMeta)
	if err != nil {
		t.Fatal(err)
	}
	if parentMeta.ID != v1Meta.ID || !reflect.DeepEqual(parent, v1) {
		t.Fatal("post-restore snapshot did not follow its explicit parent chain")
	}
	versions, err := reopened.ListVersions("store-test")
	if err != nil {
		t.Fatal(err)
	}
	if len(versions) != 3 {
		t.Fatalf("expected three persisted versions, got %d", len(versions))
	}
}

func TestPayloadHashDetectsTampering(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ledger.db")
	database, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	state := sampleState(t, nil, "manifest-v1.json", "", 1200)
	meta, err := database.Save(state)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := database.db.Exec(`UPDATE snapshots SET payload = payload || ' ' WHERE id = ?`, meta.ID); err != nil {
		t.Fatal(err)
	}
	if _, _, err := database.LoadActive("store-test"); err == nil {
		t.Fatal("expected payload hash mismatch")
	}
}

func TestMissingActiveSession(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ledger.db")
	database, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	_, _, err = database.LoadActive("missing")
	if !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("expected sql.ErrNoRows, got %v", err)
	}
}

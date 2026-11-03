package aiconfig

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// aiConfigsDDL is the Core's ai_configs table
// (core/infrastructure/persistence/database.py:67), so the store reads the
// real table shape rather than a look-alike.
const aiConfigsDDL = `
CREATE TABLE IF NOT EXISTS ai_configs (
	id TEXT PRIMARY KEY,
	name TEXT NOT NULL DEFAULT '',
	provider TEXT NOT NULL,
	model TEXT NOT NULL DEFAULT '',
	api_key_encrypted TEXT NOT NULL DEFAULT '',
	base_url TEXT NOT NULL DEFAULT '',
	system_prompt TEXT NOT NULL DEFAULT '',
	user_prompt_template TEXT NOT NULL DEFAULT '{text}',
	temperature REAL NOT NULL DEFAULT 0.7,
	is_enabled INTEGER NOT NULL DEFAULT 1,
	target_language TEXT NOT NULL DEFAULT 'en',
	owner_user_id INTEGER NOT NULL DEFAULT 0
);
`

// openTestStore builds a fresh SQLite file with the Core's ai_configs DDL
// and a working cipher, so every store test runs against the real schema.
func openTestStore(t *testing.T, masterKey string) Store {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "atf.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if _, err := db.Exec(aiConfigsDDL); err != nil {
		db.Close()
		t.Fatalf("create ai_configs: %v", err)
	}
	db.Close()
	crypto, err := NewCrypto(masterKey)
	if err != nil {
		t.Fatalf("new crypto: %v", err)
	}
	store, err := NewSQLiteStore(context.Background(), path, crypto)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	t.Cleanup(func() { _ = store.(*sqliteStore).Close() })
	return store
}

// insertConfig writes one ai_configs row the way SqliteAIConfigRepository
// does: the key column holding the column's ciphertext format.
func insertConfig(t *testing.T, store Store, row *AIConfig, masterKey string) {
	t.Helper()
	crypto, err := NewCrypto(masterKey)
	if err != nil {
		t.Fatalf("new crypto: %v", err)
	}
	enc, err := crypto.Encrypt(row.APIKey)
	if err != nil {
		t.Fatalf("encrypt key: %v", err)
	}
	s := store.(*sqliteStore)
	enabled := 0
	if row.IsEnabled {
		enabled = 1
	}
	_, err = s.db.Exec(
		`INSERT INTO ai_configs (id, name, provider, model, api_key_encrypted, base_url,
		 system_prompt, user_prompt_template, temperature, is_enabled, target_language, owner_user_id)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		row.ID, row.Name, row.Provider, row.Model, enc, row.BaseURL,
		row.SystemPrompt, row.UserPromptTemplate, row.Temperature, enabled,
		row.TargetLanguage, row.OwnerUserID,
	)
	if err != nil {
		t.Fatalf("insert ai_configs row: %v", err)
	}
}

const testMasterKey = "atf-master-key-parity-test"

func TestCryptoRoundTrip(t *testing.T) {
	crypto, err := NewCrypto(testMasterKey)
	if err != nil {
		t.Fatalf("new crypto: %v", err)
	}
	for _, plain := range []string{"", "sk-abc123", "a key with spaces and ünïcode", strings.Repeat("x", 256)} {
		token, err := crypto.Encrypt(plain)
		if err != nil {
			t.Fatalf("encrypt %q: %v", plain, err)
		}
		back, err := crypto.Decrypt(token)
		if err != nil {
			t.Fatalf("decrypt %q: %v", plain, err)
		}
		if back != plain {
			t.Errorf("round trip: got %q, want %q", back, plain)
		}
	}
}

func TestCryptoEmptyMasterKeyRefused(t *testing.T) {
	for _, key := range []string{"", "   ", "\t\n"} {
		if _, err := NewCrypto(key); err == nil {
			t.Errorf("NewCrypto(%q): expected an error, got nil", key)
		}
	}
}

func TestCryptoEmptyTokenIsEmptyKey(t *testing.T) {
	crypto, _ := NewCrypto(testMasterKey)
	got, err := crypto.Decrypt("")
	if err != nil {
		t.Fatalf("decrypt empty: %v", err)
	}
	if got != "" {
		t.Errorf("decrypt empty: got %q, want empty", got)
	}
}

func TestCryptoWrongMasterKeyIsTypedError(t *testing.T) {
	a, _ := NewCrypto("key-a")
	b, _ := NewCrypto("key-b")
	token, err := a.Encrypt("secret")
	if err != nil {
		t.Fatalf("encrypt: %v", err)
	}
	if _, err := b.Decrypt(token); err == nil {
		t.Fatal("decrypt under a different master key: expected an error")
	} else {
		var de *DecryptionError
		if !errors.As(err, &de) {
			t.Errorf("expected *DecryptionError, got %T: %v", err, err)
		}
	}
}

func TestCryptoShortCiphertextIsTypedError(t *testing.T) {
	crypto, _ := NewCrypto(testMasterKey)
	// 12 bytes of nonce and nothing else: too short to hold a tag.
	tooShort := "AAAAAAAAAAAAAAAA"
	if _, err := crypto.Decrypt(tooShort); err == nil {
		t.Fatal("decrypt short token: expected an error")
	}
}

func TestCryptoMalformedBase64IsTypedError(t *testing.T) {
	crypto, _ := NewCrypto(testMasterKey)
	for _, bad := range []string{"not!!base64!!", "###"} {
		if _, err := crypto.Decrypt(bad); err == nil {
			t.Fatalf("decrypt %q: expected an error", bad)
		}
	}
}

// TestCryptoMatchesPythonFormat pins the wire format against the Python
// CryptoService: a token the Python service encrypts must decrypt here.
// The vector below was produced by
// CryptoService("atf-master-key-parity-test").encrypt("sk-python-wrote-this").
func TestCryptoMatchesPythonFormat(t *testing.T) {
	const pythonToken = "stxkRtiQbVOzuyWdP4E7DP34_NKTdjWDY56vJOIXLKbEatx7_evtl8yjM5HW2G3m"
	crypto, err := NewCrypto(testMasterKey)
	if err != nil {
		t.Fatalf("new crypto: %v", err)
	}
	got, err := crypto.Decrypt(pythonToken)
	if err != nil {
		t.Fatalf("decrypt the Python-produced token: %v", err)
	}
	const want = "sk-python-wrote-this"
	if got != want {
		t.Errorf("python token: got %q, want %q", got, want)
	}
}

func TestStoreGetOwned(t *testing.T) {
	store := openTestStore(t, testMasterKey)
	row := &AIConfig{
		ID: "cfg-1", Name: "work", Provider: "openai", Model: "gpt-4o-mini",
		APIKey: "sk-owned", BaseURL: "https://api.openai.com/v1",
		SystemPrompt: "edit", UserPromptTemplate: "{text}",
		Temperature: 0.7, IsEnabled: true, TargetLanguage: "en",
		OwnerUserID: 42,
	}
	insertConfig(t, store, row, testMasterKey)

	got, err := store.GetOwned(context.Background(), "cfg-1", 42)
	if err != nil {
		t.Fatalf("GetOwned: %v", err)
	}
	if got == nil {
		t.Fatal("GetOwned: config not found for its owner")
	}
	if got.APIKey != "sk-owned" {
		t.Errorf("APIKey: got %q, want the decrypted plaintext", got.APIKey)
	}
	if got.Provider != "openai" || got.Model != "gpt-4o-mini" || !got.IsEnabled {
		t.Errorf("projection: got %+v", got)
	}
}

func TestStoreGetOwnedRefusesForeignOwner(t *testing.T) {
	store := openTestStore(t, testMasterKey)
	insertConfig(t, store, &AIConfig{ID: "cfg-1", Provider: "openai", APIKey: "k", OwnerUserID: 42}, testMasterKey)

	got, err := store.GetOwned(context.Background(), "cfg-1", 7)
	if err != nil {
		t.Fatalf("GetOwned foreign: %v", err)
	}
	if got != nil {
		t.Errorf("GetOwned foreign owner: got a config, want nil — a rule that names another tenant's config must not materialise it")
	}
}

func TestStoreGetOwnedMissingIsNilNotError(t *testing.T) {
	store := openTestStore(t, testMasterKey)
	got, err := store.GetOwned(context.Background(), "nope", 42)
	if err != nil {
		t.Fatalf("GetOwned missing: %v", err)
	}
	if got != nil {
		t.Errorf("GetOwned missing: got %v, want nil", got)
	}
	// An empty id is not a lookup at all.
	if got, err := store.GetOwned(context.Background(), "", 42); err != nil || got != nil {
		t.Errorf("GetOwned empty id: got %v, %v", got, err)
	}
}

func TestStoreGetByIDWorksWithoutOwnerScope(t *testing.T) {
	store := openTestStore(t, testMasterKey)
	insertConfig(t, store, &AIConfig{ID: "cfg-9", Provider: "9router", Model: "coding", APIKey: "k", OwnerUserID: 42}, testMasterKey)
	got, err := store.GetByID(context.Background(), "cfg-9")
	if err != nil {
		t.Fatalf("GetByID: %v", err)
	}
	if got == nil || got.OwnerUserID != 42 {
		t.Errorf("GetByID: got %v", got)
	}
}

func TestStoreUndecryptableRowIsAnError(t *testing.T) {
	store := openTestStore(t, testMasterKey)
	s := store.(*sqliteStore)
	_, err := s.db.Exec(`INSERT INTO ai_configs (id, provider, api_key_encrypted) VALUES ('bad', 'openai', 'not-a-token')`)
	if err != nil {
		t.Fatalf("insert: %v", err)
	}
	if _, err := store.GetByID(context.Background(), "bad"); err == nil {
		t.Fatal("undecryptable row: expected an error, got nil")
	}
}

func TestEffectiveDefaults(t *testing.T) {
	c := &AIConfig{}
	if got := c.EffectiveSystemPrompt(); got != DefaultSystemPrompt {
		t.Errorf("EffectiveSystemPrompt: got %q", got)
	}
	if got := c.EffectiveUserPromptTemplate(); got != DefaultUserPromptTemplate {
		t.Errorf("EffectiveUserPromptTemplate: got %q", got)
	}
	set := &AIConfig{SystemPrompt: " custom ", UserPromptTemplate: "translate {text}"}
	if got := set.EffectiveSystemPrompt(); got != "custom" {
		t.Errorf("EffectiveSystemPrompt: got %q", got)
	}
	if got := set.EffectiveUserPromptTemplate(); got != "translate {text}" {
		t.Errorf("EffectiveUserPromptTemplate: got %q", got)
	}
}

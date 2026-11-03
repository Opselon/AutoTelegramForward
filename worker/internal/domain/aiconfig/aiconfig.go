// Package aiconfig is the AI provider configuration aggregate and its
// owner-scoped store.
//
// The Worker reads ai_configs to run an AI rewrite at delivery time. The
// table is written by the Python Core (SqliteAIConfigRepository), and —
// like filter_rules — it lives in SQLite only: no PostgreSQL DDL in the
// Core declares it (core/main.py:183 composes SqliteAIConfigRepository
// unconditionally), so a store that read it from PostgreSQL would be
// reading a table that does not exist.
//
// The API key column is ciphertext at rest: AES-256-GCM with a key derived
// from ATF_MASTER_KEY. The Worker decrypts it only to hand it to a provider
// adapter inside the process; it is never logged, never echoed into an
// error, and never written back.
package aiconfig

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
)

// AIConfig is the execution-relevant projection of an ai_configs row.
//
// Only the fields the provider adapters and the rewrite orchestrator consume
// are carried. The Python AIConfig entity (core/domain/entities.py:2414)
// owns the business state; this is the read-only execution slice, the same
// discipline rule.ForwardRule follows.
type AIConfig struct {
	ID      string
	Name    string
	Provider string
	Model   string
	APIKey  string // plaintext, in-memory only; never logged
	BaseURL string
	SystemPrompt string
	UserPromptTemplate string
	Temperature   float64
	IsEnabled     bool
	TargetLanguage string
	OwnerUserID   int64
}

// DefaultSystemPrompt is AIConfig's own class default (entities.py:2423):
// the prompt a config that never set one ships with.
const DefaultSystemPrompt = "You are an intelligent editor. Clean up and improve the text while keeping the original meaning."

// DefaultUserPromptTemplate is AIConfig's own class default
// (entities.py:2427): a template that passes the text through unchanged.
const DefaultUserPromptTemplate = "{text}"

// DefaultTemperature is AIConfig's class default (entities.py:2428).
const DefaultTemperature = 0.7

// EffectiveSystemPrompt answers the system prompt a rewrite actually uses:
// the config's own, or the entity default when it was never set. The
// transformer's prompt builder consults this rather than the raw field so a
// hand-edited empty row behaves the way a freshly created one does.
func (c *AIConfig) EffectiveSystemPrompt() string {
	if c == nil {
		return DefaultSystemPrompt
	}
	if s := trim(c.SystemPrompt); s != "" {
		return s
	}
	return DefaultSystemPrompt
}

// EffectiveUserPromptTemplate answers the user prompt template a rewrite
// uses, with the entity's own default for a config that never set one.
func (c *AIConfig) EffectiveUserPromptTemplate() string {
	if c == nil {
		return DefaultUserPromptTemplate
	}
	if s := trim(c.UserPromptTemplate); s != "" {
		return s
	}
	return DefaultUserPromptTemplate
}

// Crypto is the AES-256-GCM at-rest cipher, a verbatim port of
// core/infrastructure/security/crypto.py::CryptoService.
//
// The wire format is urlsafe-base64(nonce || ciphertext) with a 12-byte
// nonce and no additional data. A row written by the Python Core decrypts
// here byte for byte, and a row written here decrypts in Python — the two
// services share one master key, so they must share one format.
type Crypto struct {
	key []byte
}

// NewCrypto derives the AES key from the configured master key. An empty
// master key is refused: the Python service refuses it too
// (crypto.py:25), because a derived-all-zeros key would silently encrypt
// every secret in the database under a key anyone can compute.
func NewCrypto(masterKey string) (*Crypto, error) {
	if trim(masterKey) == "" {
		return nil, errors.New("aiconfig: master key must not be empty")
	}
	sum := sha256.Sum256([]byte(masterKey))
	return &Crypto{key: sum[:]}, nil
}

// Encrypt encrypts plaintext into the urlsafe-base64(nonce||ciphertext)
// format the ai_configs.api_key_encrypted column stores.
func (c *Crypto) Encrypt(plaintext string) (string, error) {
	block, err := aes.NewCipher(c.key)
	if err != nil {
		return "", fmt.Errorf("aiconfig: build cipher: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", fmt.Errorf("aiconfig: build gcm: %w", err)
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return "", fmt.Errorf("aiconfig: nonce: %w", err)
	}
	ct := gcm.Seal(nil, nonce, []byte(plaintext), nil)
	return base64.RawURLEncoding.EncodeToString(append(nonce, ct...)), nil
}

// Decrypt reads the column format back to plaintext. An empty token is an
// empty key (crypto.py:37), and every other failure is a DecryptionError:
// a row written under a different master key, or a truncated value, is a
// one-row data problem and the caller must be able to name it without
// parsing a base64 traceback.
func (c *Crypto) Decrypt(token string) (string, error) {
	value := trim(token)
	if value == "" {
		return "", nil
	}
	// urlsafe_b64decode rejects missing padding, and a token stored by an
	// older build (or hand-edited) is exactly what produced
	// "binascii.Error: Incorrect padding" from a *list* endpoint: one bad
	// row took down the whole page. Pad locally (crypto.py:43).
	padded := value
	if pad := len(value) % 4; pad != 0 {
		padded += strings.Repeat("=", 4-pad)
	}
	raw, err := base64.URLEncoding.DecodeString(padded)
	if err != nil {
		return "", &DecryptionError{Reason: "ciphertext is not valid base64", Cause: err}
	}
	if len(raw) < 13 {
		return "", &DecryptionError{Reason: "ciphertext is too short to hold a nonce and tag"}
	}
	block, err := aes.NewCipher(c.key)
	if err != nil {
		return "", fmt.Errorf("aiconfig: build cipher: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", fmt.Errorf("aiconfig: build gcm: %w", err)
	}
	nonce, ct := raw[:12], raw[12:]
	pt, err := gcm.Open(nil, nonce, ct, nil)
	if err != nil {
		return "", &DecryptionError{Reason: "ciphertext cannot be decrypted with this master key", Cause: err}
	}
	return string(pt), nil
}

// DecryptionError is the typed failure a stored ciphertext produces, the
// port of crypto.py::DecryptionError. A failure to read one row must be
// reportable without leaking how the cipher failed.
type DecryptionError struct {
	Reason string
	Cause  error
}

func (e *DecryptionError) Error() string {
	if e.Cause != nil {
		return "aiconfig: " + e.Reason + ": " + e.Cause.Error()
	}
	return "aiconfig: " + e.Reason
}

func (e *DecryptionError) Unwrap() error { return e.Cause }

// trim is the Python str.strip() the crypto service applies before the
// empty check, so a whitespace-only token is as inert as an empty one.
func trim(s string) string {
	for len(s) > 0 && (s[0] == ' ' || s[0] == '\t' || s[0] == '\n' || s[0] == '\r') {
		s = s[1:]
	}
	for len(s) > 0 && (s[len(s)-1] == ' ' || s[len(s)-1] == '\t' || s[len(s)-1] == '\n' || s[len(s)-1] == '\r') {
		s = s[:len(s)-1]
	}
	return s
}

// Store is the read-only, owner-scoped ai_configs access the rewrite
// orchestrator needs. The Worker never writes this table.
type Store interface {
	// GetOwned returns the config when the rule's owner owns it, or nil.
	// Scoping in SQL (not read-then-compare) means a foreign row is never
	// materialised, so no caller can accidentally hold a config it does
	// not own.
	GetOwned(ctx context.Context, configID string, ownerUserID int64) (*AIConfig, error)
	// GetByID returns the config regardless of owner. Used only to name
	// WHY an owner-scoped read refused (the Python _resolve_config's
	// get_by_id fallthrough), never to execute a rewrite.
	GetByID(ctx context.Context, configID string) (*AIConfig, error)
}

package aiprovider

import (
	"bytes"
	"encoding/json"
)

// jsonMarshal encodes a request payload.
func jsonMarshal(payload any) ([]byte, error) {
	return json.Marshal(payload)
}

// jsonUnmarshal decodes a provider response into the loose shape every
// adapter mines with its own accessor (choices[0].message.content,
// content[0].text, candidates[0]…). map[string]any is deliberate: a
// strict response struct would fail every provider that adds a field,
// exactly the fragility Python's data["choices"][0] avoids.
func jsonUnmarshal(raw []byte) (map[string]any, error) {
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// byteReader wraps the request body so the HTTP client can stream a
// []byte without the caller allocating a strings.Reader import.
func byteReader(body []byte) *bytes.Reader {
	return bytes.NewReader(body)
}

// readAll reads a response body to memory, bounded by the transport's
// own limits: a provider response is a single JSON document, and the
// client already discarded anything stream-shaped by classifying it.
func readAll(body interface{ Read([]byte) (int, error) }) ([]byte, error) {
	buf := bytes.Buffer{}
	for {
		chunk := make([]byte, 32*1024)
		n, err := body.Read(chunk)
		if n > 0 {
			buf.Write(chunk[:n])
		}
		if err != nil {
			if isEOF(err) {
				break
			}
			return nil, err
		}
	}
	return buf.Bytes(), nil
}

func isEOF(err error) bool {
	return err.Error() == "EOF"
}

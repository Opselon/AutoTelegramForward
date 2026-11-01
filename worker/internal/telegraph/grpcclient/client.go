// Package grpcclient adapts the generated TelegramGateway gRPC stub to the
// worker's telegraph.Gateway port.
//
// This is the only place in the worker that knows a protobuf type exists.
// Everything upstream works against the business-neutral interface, so the
// contract can evolve (a v2 with streaming media, for example) by changing
// this adapter and nothing else.
//
// Error model: a *Telegram* refusal is a typed TelegramOperationError field
// INSIDE the response; a *transport* failure (runtime unreachable, deadline
// exceeded) is a gRPC-level error. The two must stay distinguishable,
// because only the transport one is ambiguous ("did Telegram get it?").
//
// Security: every call carries the shared internal RPC token as a bearer
// credential, and the runtime is expected to refuse calls without it. An
// empty token is a startup error, never silently allowed. gRPC metadata
// carries correlation ids only — never message content, session strings, api
// hashes or tokens.
package grpcclient

import (
	"context"
	"fmt"
	"strings"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	pb "atf/worker/proto/gen"
	"atf/worker/internal/telegraph"
)

// contractTag is stamped into every call's metadata so the runtime can
// refuse a client speaking a contract it does not implement. It must match
// the `<<contract: tfgw/v1>>` line in the .proto file.
const contractTag = "tfgw/v1"

// Client is a telegraph.Gateway backed by the gRPC Python Telegram Runtime.
type Client struct {
	conn    *grpc.ClientConn
	gateway pb.TelegramGatewayClient
	token   string
	worker  string
	timeout time.Duration
}

// New dials the Python Telegram Runtime.
//
// The token is the shared internal RPC secret; an empty token is refused
// here rather than producing a client that authenticates with nothing.
func New(addr, token, workerID string, timeout time.Duration) (*Client, error) {
	if strings.TrimSpace(addr) == "" {
		return nil, fmt.Errorf("telegram runtime address is empty")
	}
	if strings.TrimSpace(token) == "" {
		return nil, fmt.Errorf("internal RPC token is empty: refusing an unauthenticated client")
	}
	conn, err := grpc.NewClient(addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return nil, fmt.Errorf("dial telegram runtime %s: %w", addr, err)
	}
	return &Client{
		conn:    conn,
		gateway: pb.NewTelegramGatewayClient(conn),
		token:   token,
		worker:  workerID,
		timeout: timeout,
	}, nil
}

// Close releases the underlying connection.
func (c *Client) Close() error { return c.conn.Close() }

// ctxWithMeta attaches the internal auth token and correlation metadata.
//
// Metadata carries ONLY identifiers (job id, rule id, chat ids, correlation
// id, worker id). Credentials and message content never ride along.
func (c *Client) ctxWithMeta(ctx context.Context, correlationID, jobID string, fields map[string]string) (context.Context, context.CancelFunc) {
	if c.timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, c.timeout)
		return metadata.AppendToOutgoingContext(ctx,
			"authorization", "Bearer "+c.token,
			"x-atf-contract", contractTag,
			"x-atf-correlation-id", correlationID,
			"x-atf-worker-id", c.worker,
			"x-atf-job-id", jobID,
		), cancel
	}
	return metadata.AppendToOutgoingContext(ctx,
		"authorization", "Bearer "+c.token,
		"x-atf-contract", contractTag,
		"x-atf-correlation-id", correlationID,
		"x-atf-worker-id", c.worker,
		"x-atf-job-id", jobID,
	), func() {}
}

// job identifies the operation for the metadata + RequestMeta builders.
type job struct {
	correlationID string
	id            string
	fields        map[string]string
}

func newJob(correlationID, jobID string, extra map[string]string) job {
	fields := map[string]string{}
	for k, v := range extra {
		fields[k] = v
	}
	return job{correlationID: correlationID, id: jobID, fields: fields}
}

func (j job) meta() *pb.RequestMeta {
	return &pb.RequestMeta{
		CorrelationId: j.correlationID,
		JobId:         j.id,
		Fields:        j.fields,
	}
}

// transportFailure is the failure returned for a gRPC-level error: the
// runtime was unreachable or the deadline ran out, so the outcome on
// Telegram's side is unknown — never "not sent".
func transportFailure(err error) *telegraph.DeliveryFailure {
	return &telegraph.DeliveryFailure{
		Code:      "NETWORK_TIMEOUT",
		ErrorType: "UNAVAILABLE",
		Message:   err.Error(),
	}
}

// opFailure converts the runtime's typed TelegramOperationError into the
// worker's DeliveryFailure vocabulary. nil in means nil out.
func opFailure(e *pb.TelegramOperationError) *telegraph.DeliveryFailure {
	if e == nil {
		return nil
	}
	var code string
	if name, ok := pb.DeliveryFailureCode_name[int32(e.GetCode())]; ok {
		code = name
	}
	if code == "" || code == "DELIVERY_FAILURE_UNSPECIFIED" {
		code = "UNKNOWN_DELIVERY_OUTCOME"
	}
	return &telegraph.DeliveryFailure{
		Code:              code,
		ErrorType:         e.GetErrorType(),
		ErrorCode:         e.GetErrorCode(),
		RetryAfterSeconds: int(e.GetRetryAfterSeconds()),
		Message:           e.GetMessage(),
	}
}

// --- session connectivity ------------------------------------------------- //

// IsSessionReady reports whether the runtime can serve MTProto operations
// for this session right now. gRPC-level errors are returned as a Go error
// (the caller treats an unreachable runtime as "defer, do not fail").
func (c *Client) IsSessionReady(ctx context.Context, sessionID string) (bool, string, error) {
	j := newJob("", "", map[string]string{"session_id": sessionID})
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.IsSessionReady(callCtx, &pb.SessionAvailabilityRequest{
		Meta: j.meta(), SessionId: sessionID,
	})
	if err != nil {
		return false, "", err
	}
	return resp.GetReady(), resp.GetState(), nil
}

// --- reads ---------------------------------------------------------------- //

// GetMessage fetches the minimal view of one source message.
func (c *Client) GetMessage(ctx context.Context, sessionID string, chatID, messageID int64) (*telegraph.MessageSnapshot, *telegraph.DeliveryFailure) {
	j := newJob("", "", map[string]string{
		"session_id": sessionID, "chat_id": fmt.Sprintf("%d", chatID),
	})
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.GetMessage(callCtx, &pb.GetMessageRequest{
		Meta: j.meta(), SessionId: sessionID, ChatId: chatID, MessageId: messageID,
	})
	if err != nil {
		return nil, transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return nil, f
	}
	info := resp.GetInfo()
	if info == nil {
		return &telegraph.MessageSnapshot{}, nil
	}
	return &telegraph.MessageSnapshot{
		ChatID:       info.GetChatId(),
		MessageID:    info.GetMessageId(),
		MediaGroupID: info.GetMediaGroupId(),
		HasMedia:     info.GetHasMedia(),
		MediaType:    info.GetMediaType(),
	}, nil
}

// GetAlbumMessageIds resolves every member of an album.
func (c *Client) GetAlbumMessageIds(ctx context.Context, sessionID string, chatID, messageID int64) ([]int64, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.GetAlbumMessageIds(callCtx, &pb.GetAlbumMessageIdsRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		ChatId: chatID, MessageId: messageID,
	})
	if err != nil {
		return nil, transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return nil, f
	}
	return resp.GetMessageIds(), nil
}

// GetFileExtension returns the extension of a message's file ("" if none).
func (c *Client) GetFileExtension(ctx context.Context, sessionID string, chatID, messageID int64) (string, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.GetFileExtension(callCtx, &pb.GetFileExtensionRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		ChatId: chatID, MessageId: messageID,
	})
	if err != nil {
		return "", transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return "", f
	}
	return resp.GetExtension(), nil
}

// --- media staging --------------------------------------------------------- //

// DownloadMedia streams the message's file to the shared scratch path.
func (c *Client) DownloadMedia(ctx context.Context, sessionID string, chatID, messageID int64, dest telegraph.MediaHandle) (int64, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.DownloadMedia(callCtx, &pb.DownloadMediaRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		ChatId: chatID, MessageId: messageID,
		Destination: &pb.TelegramMediaHandle{Path: dest.Path, FileName: dest.FileName},
	})
	if err != nil {
		return 0, transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return 0, f
	}
	return resp.GetSizeBytes(), nil
}

// ReloadMedia downloads raw bytes for the re-upload fallback.
func (c *Client) ReloadMedia(ctx context.Context, sessionID string, chatID, messageID int64, dest telegraph.MediaHandle, mediaType string) (int64, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.ReloadMedia(callCtx, &pb.ReloadMediaRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		SourceChatId: chatID, SourceMessageId: messageID,
		Destination: &pb.TelegramMediaHandle{Path: dest.Path, FileName: dest.FileName},
		MediaType:   mediaType,
	})
	if err != nil {
		return 0, transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return 0, f
	}
	return resp.GetSizeBytes(), nil
}

// PrepareMediaUpload stages a file for upload and returns an opaque token
// the runtime turns into a file reference. Empty token = fall back to text.
func (c *Client) PrepareMediaUpload(ctx context.Context, sessionID string, source telegraph.MediaHandle, sourceChatID, sourceMessageID int64, mediaType string) (string, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.PrepareMediaUpload(callCtx, &pb.PrepareMediaUploadRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		SourceChatId: sourceChatID, SourceMessageId: sourceMessageID,
		Source:     &pb.TelegramMediaHandle{Path: source.Path, FileName: source.FileName},
		MediaType:  mediaType,
	})
	if err != nil {
		return "", transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return "", f
	}
	return resp.GetFileReferenceToken(), nil
}

// --- sends (the Telegram side effects) ------------------------------------- //

// ForwardMessages performs a native Telegram forward.
func (c *Client) ForwardMessages(ctx context.Context, sessionID string, targetChatID, fromChatID int64, messageIDs []int64, threadID int64) ([]int64, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.ForwardMessages(callCtx, &pb.ForwardMessagesRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		TargetChatId: targetChatID, FromChatId: fromChatID,
		MessageIds: messageIDs, MessageThreadId: threadID,
	})
	if err != nil {
		return nil, transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return nil, f
	}
	return resp.GetTargetMessageIds(), nil
}

// CopyMessage performs a clean copy of one message.
func (c *Client) CopyMessage(ctx context.Context, sessionID string, targetChatID, fromChatID, messageID int64, caption string, threadID int64) (int64, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.CopyMessage(callCtx, &pb.CopyMessageRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		TargetChatId: targetChatID, FromChatId: fromChatID,
		MessageId: messageID, Caption: caption, MessageThreadId: threadID,
	})
	if err != nil {
		return 0, transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return 0, f
	}
	return resp.GetTargetMessageId(), nil
}

// CopyMessages copies several messages one-for-one.
func (c *Client) CopyMessages(ctx context.Context, sessionID string, targetChatID, fromChatID int64, messageIDs []int64, caption string, threadID int64) ([]int64, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.CopyMessages(callCtx, &pb.CopyMessagesRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		TargetChatId: targetChatID, FromChatId: fromChatID,
		MessageIds: messageIDs, Caption: caption, MessageThreadId: threadID,
	})
	if err != nil {
		return nil, transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return nil, f
	}
	return resp.GetTargetMessageIds(), nil
}

// CopyMediaGroup copies an album as a group.
func (c *Client) CopyMediaGroup(ctx context.Context, sessionID string, targetChatID, fromChatID, messageID int64, caption string, threadID int64) ([]int64, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.CopyMediaGroup(callCtx, &pb.CopyMediaGroupRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		TargetChatId: targetChatID, FromChatId: fromChatID,
		MessageId: messageID, Caption: caption, MessageThreadId: threadID,
	})
	if err != nil {
		return nil, transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return nil, f
	}
	return resp.GetTargetMessageIds(), nil
}

// SendMedia uploads a staged file as new media (the re-upload fallback).
func (c *Client) SendMedia(ctx context.Context, sessionID string, targetChatID int64, token, caption, mediaType string, threadID int64) (int64, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.SendMedia(callCtx, &pb.SendMediaRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		TargetChatId: targetChatID, FileReferenceToken: token,
		Caption: caption, MediaType: mediaType, MessageThreadId: threadID,
	})
	if err != nil {
		return 0, transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return 0, f
	}
	return resp.GetTargetMessageId(), nil
}

// SendMessage sends plain transformed text.
func (c *Client) SendMessage(ctx context.Context, sessionID string, targetChatID int64, text string, threadID int64) (int64, *telegraph.DeliveryFailure) {
	callCtx, cancel := c.ctxWithMeta(ctx, "", "", nil)
	defer cancel()
	resp, err := c.gateway.SendMessage(callCtx, &pb.SendMessageRequest{
		Meta: newJob("", "", nil).meta(), SessionId: sessionID,
		TargetChatId: targetChatID, Text: text, MessageThreadId: threadID,
	})
	if err != nil {
		return 0, transportFailure(err)
	}
	if f := opFailure(resp.GetError()); f != nil {
		return 0, f
	}
	return resp.GetTargetMessageId(), nil
}

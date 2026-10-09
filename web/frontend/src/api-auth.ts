import type { AuthResponse } from './types'

export async function login(req: { username: string; password: string }): Promise<AuthResponse> {
  return fetchJson('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username: req.username, password: req.password }),
  })
}

export async function register(req: { username: string; password: string }): Promise<AuthResponse> {
  return fetchJson('/api/auth/register', {
    method: 'POST',
    body: JSON.stringify({ username: req.username, password: req.password }),
  })
}

export async function me(): Promise<{ user_id: number; username: string; is_admin: boolean; now: string }> {
  return fetchJson('/api/auth/me')
}

export async function exchangeBotToken(req: { user_id: number }): Promise<AuthResponse> {
  return fetchJson('/api/auth/exchange-bot-token', {
    method: 'POST',
    body: JSON.stringify({ user_id: req.user_id }),
  })
}

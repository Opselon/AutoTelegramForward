/**
 * How a chat list is ordered for a picker: pinned chats first.
 *
 * The bot's pickers lead with the chats a user pinned deliberately — those are
 * the ones a task is usually built around — and the dashboard and the app must
 * read the same way or "the same feature" means three different screens. The
 * rule lives here, source-only and dependency-free, so it is tested directly by
 * `tests/chatOrder.test.mjs` instead of through a rendered component.
 *
 * `pinned` is advisory: it is omitted from the API payload when false (proto3
 * omitempty), so a page from an older server simply has nothing pinned and the
 * order is the server's own — newest-first — untouched.
 */
export interface PinnedChat {
  chat_id: string
  pinned?: boolean
}

/**
 * Pinned first, everything else in the order it arrived.
 *
 * "Stable" is the whole point: the server sends newest-first, and a comparator
 * that only looks at `pinned` would let the sort reshuffle the unpinned rows on
 * every render (JS `sort` is stable only since ES2019, and stability is a
 * property of the engine rather than of the intent). The index tiebreak states
 * the intent, and an unpinned page is returned as the same array so a caller
 * cannot tell the difference between "nothing pinned" and "no sort applied".
 */
export function pinnedFirstOrder<T extends PinnedChat>(chats: T[]): T[] {
  if (!chats.some((chat) => !!chat.pinned)) return chats
  return chats
    .map((chat, index) => ({ chat, index }))
    .sort((a, b) => {
      if (!!a.chat.pinned !== !!b.chat.pinned) return a.chat.pinned ? -1 : 1
      return a.index - b.index
    })
    .map((entry) => entry.chat)
}

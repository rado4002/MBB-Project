import { useCallback, useEffect, useRef, useState } from 'react'
import type { ConversationApiClient } from '../../api/conversations'
import type {
  OperatorConversationDetail,
  OperatorMessageItem,
} from '../../api/contracts/conversations'
import { asApiError, type ApiError } from '../../api/errors'

function wasAborted(error: unknown, signal: AbortSignal) {
  return signal.aborted || (error instanceof DOMException && error.name === 'AbortError')
}

interface DetailState {
  detail: OperatorConversationDetail | null
  loading: boolean
  error: ApiError | null
}

export function useConversationDetail(
  client: ConversationApiClient,
  conversationId: string,
) {
  const [state, setState] = useState<DetailState>({
    detail: null,
    loading: true,
    error: null,
  })
  const version = useRef(0)
  const controller = useRef<AbortController | null>(null)

  const loadDetail = useCallback(async (showLoading: boolean) => {
    controller.current?.abort()
    const activeController = new AbortController()
    controller.current = activeController
    const activeVersion = ++version.current
    if (showLoading) {
      setState(() => ({ detail: null, loading: true, error: null }))
    }
    try {
      const detail = await client.getConversation(
        conversationId,
        activeController.signal,
      )
      if (activeVersion !== version.current || activeController.signal.aborted) return
      setState({ detail, loading: false, error: null })
    } catch (unknownError) {
      if (
        wasAborted(unknownError, activeController.signal) ||
        activeVersion !== version.current
      ) return
      setState({ detail: null, loading: false, error: asApiError(unknownError) })
    }
  }, [client, conversationId])

  useEffect(() => {
    // The effect starts the cancellable external request for this route key.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadDetail(false)
    return () => controller.current?.abort()
  }, [loadDetail])

  return {
    ...state,
    retry: () => loadDetail(true),
    refresh: () => loadDetail(false),
  }
}

interface HistoryState {
  items: OperatorMessageItem[]
  nextOlderCursor: string | null
  loading: boolean
  loadingOlder: boolean
  error: ApiError | null
  olderError: ApiError | null
}

export function useMessageHistory(
  client: ConversationApiClient,
  conversationId: string,
) {
  const [state, setState] = useState<HistoryState>({
    items: [],
    nextOlderCursor: null,
    loading: true,
    loadingOlder: false,
    error: null,
    olderError: null,
  })
  const version = useRef(0)
  const controller = useRef<AbortController | null>(null)

  const loadRecent = useCallback(async (showLoading: boolean) => {
    controller.current?.abort()
    const activeController = new AbortController()
    controller.current = activeController
    const activeVersion = ++version.current
    if (showLoading) {
      setState(() => ({
        items: [],
        nextOlderCursor: null,
        loading: true,
        loadingOlder: false,
        error: null,
        olderError: null,
      }))
    }
    try {
      const response = await client.getMessages(
        conversationId,
        undefined,
        activeController.signal,
      )
      if (activeVersion !== version.current || activeController.signal.aborted) return
      setState({
        items: response.items,
        nextOlderCursor: response.next_older_cursor,
        loading: false,
        loadingOlder: false,
        error: null,
        olderError: null,
      })
    } catch (unknownError) {
      if (
        wasAborted(unknownError, activeController.signal) ||
        activeVersion !== version.current
      ) return
      setState({
        items: [],
        nextOlderCursor: null,
        loading: false,
        loadingOlder: false,
        error: asApiError(unknownError),
        olderError: null,
      })
    }
  }, [client, conversationId])

  useEffect(() => {
    void loadRecent(false)
    return () => controller.current?.abort()
  }, [loadRecent])

  const loadEarlier = useCallback(async () => {
    if (!state.nextOlderCursor || state.loadingOlder) return
    controller.current?.abort()
    const activeController = new AbortController()
    controller.current = activeController
    const activeVersion = ++version.current
    const cursor = state.nextOlderCursor
    setState((current) => ({
      ...current,
      loadingOlder: true,
      olderError: null,
    }))
    try {
      const response = await client.getMessages(
        conversationId,
        cursor,
        activeController.signal,
      )
      if (activeVersion !== version.current || activeController.signal.aborted) return
      setState((current) => {
        const existing = new Set(current.items.map((item) => item.message_id))
        const uniqueOlder = response.items.filter(
          (item) => !existing.has(item.message_id),
        )
        return {
          ...current,
          items: [...uniqueOlder, ...current.items],
          nextOlderCursor: response.next_older_cursor,
          loadingOlder: false,
          olderError: null,
        }
      })
    } catch (unknownError) {
      if (
        wasAborted(unknownError, activeController.signal) ||
        activeVersion !== version.current
      ) return
      setState((current) => ({
        ...current,
        loadingOlder: false,
        olderError: asApiError(unknownError),
      }))
    }
  }, [client, conversationId, state.loadingOlder, state.nextOlderCursor])

  return {
    ...state,
    retry: () => loadRecent(true),
    loadEarlier,
  }
}

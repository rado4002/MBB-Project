import { useCallback, useEffect, useRef, useState } from 'react'
import type { ConversationApiClient } from '../../api/conversations'
import type {
  ConversationFilters,
  OperatorConversationQueueItem,
} from '../../api/contracts/conversations'
import { asApiError, type ApiError } from '../../api/errors'

interface ConversationQueueState {
  items: OperatorConversationQueueItem[]
  nextCursor: string | null
  loading: boolean
  refreshing: boolean
  loadingMore: boolean
  error: ApiError | null
}

const initialState: ConversationQueueState = {
  items: [],
  nextCursor: null,
  loading: true,
  refreshing: false,
  loadingMore: false,
  error: null,
}

function wasAborted(error: unknown, signal: AbortSignal) {
  return signal.aborted || (error instanceof DOMException && error.name === 'AbortError')
}

export function useConversationQueue(
  client: ConversationApiClient,
  filters: ConversationFilters,
) {
  const [state, setState] = useState(initialState)
  const activeController = useRef<AbortController | null>(null)
  const requestVersion = useRef(0)

  const loadFirstPage = useCallback(
    async (preserveItems: boolean) => {
      activeController.current?.abort()
      const controller = new AbortController()
      activeController.current = controller
      const version = ++requestVersion.current
      setState((current) => ({
        ...current,
        items: preserveItems ? current.items : [],
        nextCursor: preserveItems ? current.nextCursor : null,
        loading: !preserveItems,
        refreshing: preserveItems,
        loadingMore: false,
        error: null,
      }))
      try {
        const response = await client.listConversations({
          filters,
          signal: controller.signal,
        })
        if (version !== requestVersion.current || controller.signal.aborted) return
        setState({
          items: response.items,
          nextCursor: response.next_cursor,
          loading: false,
          refreshing: false,
          loadingMore: false,
          error: null,
        })
      } catch (unknownError) {
        if (wasAborted(unknownError, controller.signal) || version !== requestVersion.current) return
        setState((current) => ({
          ...current,
          loading: false,
          refreshing: false,
          loadingMore: false,
          error: asApiError(unknownError),
        }))
      }
    },
    [client, filters],
  )

  useEffect(() => {
    void loadFirstPage(false)
    return () => activeController.current?.abort()
  }, [loadFirstPage])

  const loadMore = useCallback(async () => {
    if (!state.nextCursor || state.loadingMore || state.refreshing) return
    activeController.current?.abort()
    const controller = new AbortController()
    activeController.current = controller
    const version = ++requestVersion.current
    const cursor = state.nextCursor
    setState((current) => ({ ...current, loadingMore: true, error: null }))
    try {
      const response = await client.listConversations({
        filters,
        cursor,
        signal: controller.signal,
      })
      if (version !== requestVersion.current || controller.signal.aborted) return
      setState((current) => {
        const existing = new Set(current.items.map((item) => item.conversation_id))
        const uniqueItems = response.items.filter(
          (item) => !existing.has(item.conversation_id),
        )
        return {
          ...current,
          items: [...current.items, ...uniqueItems],
          nextCursor: response.next_cursor,
          loadingMore: false,
          error: null,
        }
      })
    } catch (unknownError) {
      if (wasAborted(unknownError, controller.signal) || version !== requestVersion.current) return
      setState((current) => ({
        ...current,
        loadingMore: false,
        error: asApiError(unknownError),
      }))
    }
  }, [client, filters, state.loadingMore, state.nextCursor, state.refreshing])

  return {
    ...state,
    refresh: () => loadFirstPage(true),
    retry: () => loadFirstPage(state.items.length > 0),
    loadMore,
  }
}

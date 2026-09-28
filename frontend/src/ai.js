export const initialAIState = {
  status: null,
  messages: [],
  activeId: null,
  beforeCode: null,
  changed: false,
}

export function aiReducer(state, action) {
  switch (action.type) {
    case 'status':
      return { ...state, status: action.status }
    case 'prompt':
      return {
        ...state,
        messages: [...state.messages, { role: 'user', id: action.id, text: action.text }],
        activeId: action.id,
        beforeCode: action.code,
        changed: false,
      }
    case 'event': {
      if (action.id !== state.activeId) return state
      const { event } = action
      if (event.kind === 'text' || event.kind === 'thinking') {
        const messages = [...state.messages]
        let last = messages.at(-1)
        if (last?.role !== 'assistant') {
          last = { role: 'assistant', id: action.id, text: '', thinking: '' }
          messages.push(last)
        } else {
          last = { ...last }
          messages[messages.length - 1] = last
        }
        if (event.kind === 'text') last.text += event.delta || ''
        else last.thinking += event.delta || ''
        return { ...state, messages }
      }
      if (event.kind === 'tool') {
        const index = state.messages.findIndex((message) => message.role === 'tool' && message.callId === event.call_id)
        const tool = {
          role: 'tool', callId: event.call_id, name: event.name, phase: event.phase,
          summary: event.summary, isError: Boolean(event.is_error),
        }
        const messages = [...state.messages]
        if (index < 0) messages.push(tool)
        else messages[index] = { ...messages[index], ...tool }
        return { ...state, messages }
      }
      return state
    }
    case 'code':
      if (action.id !== state.activeId) return state
      return { ...state, changed: action.code !== state.beforeCode }
    case 'done':
      if (action.id !== state.activeId) return state
      return {
        ...state,
        activeId: null,
        changed: typeof action.code === 'string' ? action.code !== state.beforeCode : state.changed,
        messages: action.ok ? state.messages : [...state.messages, { role: 'error', id: action.id, text: action.error || 'The AI request failed.' }],
      }
    case 'new-chat':
      return { ...state, messages: [], activeId: null, beforeCode: null, changed: false }
    case 'revert':
      return { ...state, changed: false, beforeCode: null }
    default:
      return state
  }
}

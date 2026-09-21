export function createOperation(kind, context, now = Date.now()) {
  return {
    kind,
    requestId: context.request_id,
    revisionId: context.revision_id,
    seq: context.seq,
    stage: 'queued',
    startedAt: now,
  }
}

export function operationMatches(message, operation, includeSeq = false) {
  if (!message || !operation) return false
  if (message.request_id !== operation.requestId || message.revision_id !== operation.revisionId) return false
  return !includeSeq || message.seq === operation.seq
}

export function updateOperation(operation, message) {
  if (!operationMatches(message, operation, operation.kind === 'preview')) return false
  if (message.stage) operation.stage = message.stage
  return true
}

export function elapsedText(milliseconds) {
  return `${(Math.max(0, milliseconds) / 1000).toFixed(1)}s`
}

export function stageText(stage, fallback = 'working…') {
  return {
    queued: 'queued',
    building: 'building',
    tessellating: 'tessellating',
    exporting: 'exporting',
    'open-request': 'sending to plate',
    duplicate: 'already running',
  }[stage] || fallback
}

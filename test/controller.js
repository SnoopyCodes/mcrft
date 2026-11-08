'use strict'

const { performance } = require('node:perf_hooks')
const { createObservationCalculator } = require('./observations')

// Keep sampling and copying on the JS side: Python receives plain JSON, never
// live entity proxies that could change while a delayed observation is read.
function createController (bot, { delayMs = 250, arenaCenter = null } = {}, now = () => performance.now()) {
  if (!Number.isFinite(delayMs) || delayMs < 0) {
    throw new RangeError('delayMs must be a finite, nonnegative number')
  }

  let active = false
  let closed = false
  let tick = 0
  let pending = []
  let released = null
  let spawned = false
  let paused = false
  let generation = 0
  let episodeId = null
  let opponentUsername = null
  let teleportCount = 0
  const calculator = arenaCenter === null ? null : createObservationCalculator(arenaCenter)

  function clear () {
    generation++
    active = false
    pending = []
    released = null
    calculator?.reset()
    bot.clearControlStates()
  }

  function spawn () {
    clear()
    spawned = true
    active = !paused
  }

  function despawn () { spawned = false; clear() }
  function forcedMove () {
    teleportCount++
    // A teleport is a discontinuity, not velocity; discard delayed old poses too.
    pending = []
    released = null
    calculator?.reset()
  }

  function entityHurt (entity) {
    if (active && bot.entity) calculator?.recordHurt(entity.id, bot.entity.id, tick + 1)
  }

  // Setup-only telemetry: never feed this immediate state to a policy.
  function getLifecycleJSON () {
    const p = bot.entity?.position
    return JSON.stringify({ spawned, teleport_count: teleportCount,
      position: p ? { x: p.x, y: p.y, z: p.z } : null })
  }

  function prepareEpisodeJSON (optionsJSON) {
    const options = JSON.parse(optionsJSON)
    paused = true
    clear()
    episodeId = options.episode_id
    opponentUsername = options.opponent_username
  }

  function resumeEpisode () {
    if (closed || !spawned) throw new Error('Bot is not spawned')
    clear()
    paused = false
    active = true
  }

  function release (timestamp) {
    // Retain only the newest eligible snapshot and the still-delayed ones.
    let count = 0
    while (count < pending.length && pending[count].sampled_at_ms + delayMs <= timestamp) {
      released = pending[count++]
    }
    if (count) pending.splice(0, count)
  }

  function pose (entity) {
    return {
      id: entity.id,
      type: entity.type ?? null,
      username: entity.username ?? null,
      position: { x: entity.position.x, y: entity.position.y, z: entity.position.z },
      yaw: entity.yaw,
      pitch: entity.pitch,
      on_ground: typeof entity.onGround === 'boolean' ? entity.onGround : null,
      head_yaw: entity === bot.entity ? entity.yaw : (entity.headYaw ?? entity.yaw)
    }
  }

  function capture () {
    if (!active || !bot.entity?.position) return
    const timestamp = now()
    const nearest = bot.nearestEntity(entity => entity.isValid !== false && entity.type === 'player' &&
      (opponentUsername === null || entity.username === opponentUsername))
    const snapshot = {
      episode_id: episodeId,
      tick: ++tick,
      tick_source: 'client_physics',
      // World age is only updated when the server sends a time packet.
      server_world_age_at_last_sync: bot.time?.age ?? null,
      sampled_at_ms: timestamp,
      self: pose(bot.entity),
      closest_entity: nearest ? pose(nearest) : null
    }
    snapshot.observation = calculator ? calculator.sample(snapshot) : null
    pending.push(snapshot)
    release(timestamp)
  }

  function getStateJSON () {
    if (closed || !active) return 'null'
    const timestamp = now()
    release(timestamp)
    if (!released) return 'null'
    return JSON.stringify({ ...released, delay_ms: delayMs, age_ms: timestamp - released.sampled_at_ms })
  }

  async function applyActionJSON (actionJSON) {
    const action = JSON.parse(actionJSON)
    const { forward = 0, strafe = 0, jump = false, sprint = false,
      delta_yaw: deltaYaw = 0, delta_pitch: deltaPitch = 0, attack = false } = action
    if (![-1, 0, 1].includes(forward) || ![-1, 0, 1].includes(strafe)) {
      throw new RangeError('forward and strafe must be -1, 0, or 1')
    }
    if (![jump, sprint, attack].every(value => typeof value === 'boolean')) {
      throw new TypeError('jump, sprint, and attack must be booleans')
    }
    if (![deltaYaw, deltaPitch].every(Number.isFinite)) {
      throw new TypeError('aim deltas must be finite radians')
    }
    if (closed || !active || !bot.entity) throw new Error('Bot is not spawned')
    const actionGeneration = generation

    // Each call replaces all held keys. Negative strafe = A, positive = D.
    for (const [key, held] of Object.entries({
      forward: forward === 1, back: forward === -1,
      left: strafe === -1, right: strafe === 1, jump, sprint
    })) bot.setControlState(key, held)

    if (deltaYaw !== 0 || deltaPitch !== 0) {
      const yaw = bot.entity.yaw + deltaYaw
      const pitch = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, bot.entity.pitch + deltaPitch))
      await bot.look(yaw, pitch, true)
    }

    if (attack && active && !closed && generation === actionGeneration) {
      const target = bot.entityAtCursor(3)
      if (target && target.isValid !== false) {
        calculator?.recordAttack(target.id, tick + 1)
        bot.attack(target)
      } else bot.swingArm()
    }
    // Do not return a live target or success signal that bypasses the delay.
  }

  const listeners = {
    spawn,
    death: despawn,
    respawn: despawn,
    end: despawn,
    forcedMove,
    entityHurt,
    physicsTick: capture
  }
  for (const [event, handler] of Object.entries(listeners)) bot.on(event, handler)

  function close () {
    clear()
    closed = true
    for (const [event, handler] of Object.entries(listeners)) bot.removeListener(event, handler)
  }

  return { applyActionJSON, getStateJSON, prepareEpisodeJSON, resumeEpisode,
    stopControls: () => bot.clearControlStates(),
    getLifecycleJSON, close }
}

module.exports = { createController }

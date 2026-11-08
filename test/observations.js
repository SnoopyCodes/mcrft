'use strict'

// All angles use Mineflayer radians: yaw 0 = -Z, +yaw turns toward -X;
// +pitch looks up. Horizontal polar coordinates retain Y as a separate height.
const wrap = angle => ((angle + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI
const subtract = (a, b) => ({ x: a.x - b.x, y: a.y - b.y, z: a.z - b.z })
const bearing = v => v.x === 0 && v.z === 0 ? 0 : Math.atan2(-v.x, -v.z)

function polarPosition (offset, referenceYaw = 0) {
  const radius = Math.hypot(offset.x, offset.z)
  return { radius, yaw: radius === 0 ? 0 : wrap(bearing(offset) - referenceYaw), height: offset.y }
}

function polarVelocity (velocity, offset) {
  if (velocity === null) return null
  const yaw = bearing(offset)
  return {
    radial: -Math.sin(yaw) * velocity.x - Math.cos(yaw) * velocity.z,
    tangential: -Math.cos(yaw) * velocity.x + Math.sin(yaw) * velocity.z,
    vertical: velocity.y
  }
}

function relativeAim (entity, offset) {
  return {
    yaw: wrap(entity.head_yaw - bearing(offset)),
    pitch: wrap(entity.pitch - Math.atan2(offset.y, Math.hypot(offset.x, offset.z)))
  }
}

function createObservationCalculator (center) {
  if (!center || !['x', 'y', 'z'].every(axis => Number.isFinite(center[axis]))) {
    throw new TypeError('arena center must contain finite x, y, z coordinates')
  }
  center = { ...center }
  const historyTicks = 40
  const hitWindowTicks = 6
  let history, attacks, lastGottenHit, lastHit

  function reset () {
    history = []
    attacks = []
    lastGottenHit = null
    lastHit = null
  }
  reset()

  function trimAttacks (tick) {
    attacks = attacks.filter(attack => tick - attack.tick <= hitWindowTicks)
  }

  function recordAttack (id, tick) {
    trimAttacks(tick)
    attacks.push({ id, tick })
    // Also bound memory if many actions arrive between physics ticks.
    if (attacks.length > 64) attacks.shift()
  }

  function recordHurt (id, selfId, tick) {
    if (id === selfId) lastGottenHit = tick
    trimAttacks(tick)
    if (id !== selfId && attacks.some(attack => attack.id === id)) {
      lastHit = { id, tick }
      attacks = attacks.filter(attack => attack.id !== id)
    }
  }

  function sample (snapshot) {
    const { tick, self, closest_entity: opponent } = snapshot
    const previous = history.at(-1)
    // Positions are sampled every physics tick, even if Python polls less often.
    // Units are blocks/client tick, independent of wall-clock stalls/catch-up.
    const consecutive = previous && tick === previous.tick + 1
    const selfVelocity = consecutive && previous.self.id === self.id
      ? subtract(self.position, previous.self.position) : null
    const opponentVelocity = selfVelocity && opponent && previous.closest_entity?.id === opponent.id
      ? subtract(subtract(opponent.position, previous.closest_entity.position), selfVelocity) : null
    const fromCenter = subtract(self.position, center)
    const toOpponent = opponent ? subtract(opponent.position, self.position) : null
    const observation = {
      self_position: polarPosition(fromCenter),
      self_velocity: polarVelocity(selfVelocity, fromCenter),
      self_aim_to_center: relativeAim(self, subtract(center, self.position)),
      opponent_position: opponent ? polarPosition(toOpponent, self.yaw) : null,
      opponent_velocity: opponent ? polarVelocity(opponentVelocity, toOpponent) : null,
      opponent_aim_to_self: opponent ? relativeAim(opponent, subtract(self.position, opponent.position)) : null,
      on_ground: self.on_ground,
      ticks_since_gotten_hit: lastGottenHit === null ? null : tick - lastGottenHit,
      ticks_since_hit_on_opponent: opponent && lastHit?.id === opponent.id ? tick - lastHit.tick : null
    }
    // Retain detached poses only, not full snapshots containing observations.
    history.push({ tick, self, closest_entity: opponent })
    if (history.length > historyTicks) history.shift()
    trimAttacks(tick)
    return observation
  }

  return { sample, reset, recordAttack, recordHurt }
}

module.exports = { createObservationCalculator }

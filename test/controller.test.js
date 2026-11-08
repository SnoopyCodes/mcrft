'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { createController } = require('./controller')

function fixture (delayMs = 250, options = {}) {
  let timestamp = 0
  const bot = new EventEmitter()
  function entity (id, x) {
    return { id, type: 'player', username: `player${id}`, isValid: true,
      position: { x, y: 64, z: 0 }, yaw: 0, pitch: 0, headYaw: 0.5, onGround: true }
  }
  bot.entity = entity(1, 0)
  bot.entities = [bot.entity, entity(2, 2), entity(3, 5)]
  bot.time = { age: 1000 }
  bot.controls = {}
  bot.calls = []
  bot.clearControlStates = () => { bot.controls = {} }
  bot.setControlState = (key, held) => { bot.controls[key] = held }
  bot.nearestEntity = match => bot.entities
    .filter(e => e !== bot.entity && match(e))
    .sort((a, b) => Math.abs(a.position.x - bot.entity.position.x) -
      Math.abs(b.position.x - bot.entity.position.x))[0] ?? null
  bot.look = async (yaw, pitch, force) => {
    bot.calls.push(['look', yaw, pitch, force])
    bot.entity.yaw = yaw
    bot.entity.pitch = pitch
  }
  bot.target = null
  bot.entityAtCursor = reach => {
    bot.calls.push(['cursor', reach])
    return bot.target
  }
  bot.attack = target => bot.calls.push(['attack', target.id])
  bot.swingArm = () => bot.calls.push(['swing'])
  const controller = createController(bot, { delayMs, ...options }, () => timestamp)
  const state = () => JSON.parse(controller.getStateJSON())
  const action = value => controller.applyActionJSON(JSON.stringify(value))
  const at = value => { timestamp = value }
  bot.emit('spawn')
  return { bot, controller, state, action, at }
}

test('observations never appear before the delay, including at startup', () => {
  const { bot, state, at } = fixture()
  assert.equal(state(), null)
  bot.emit('physicsTick')
  at(249)
  assert.equal(state(), null)
  at(250)
  const snapshot = state()
  assert.equal(snapshot.tick, 1)
  assert.equal(snapshot.tick_source, 'client_physics')
  assert.equal(snapshot.server_world_age_at_last_sync, 1000)
  assert.equal(snapshot.age_ms, 250)
  assert.equal(snapshot.delay_ms, 250)
})

test('positions, aim, closest selection and tick stay historical', () => {
  const { bot, state, at } = fixture()
  bot.emit('physicsTick')
  at(50)
  bot.entity.position.x = 10
  bot.entity.yaw = 1
  bot.entities[1].position.x = 20
  bot.entities[1].headYaw = 2
  bot.time.age = 1020
  bot.emit('physicsTick')
  at(250)
  const old = state()
  assert.equal(old.self.position.x, 0)
  assert.equal(old.self.yaw, 0)
  assert.equal(old.closest_entity.id, 2)
  assert.equal(old.closest_entity.position.x, 2)
  assert.equal(old.closest_entity.head_yaw, 0.5)
  assert.equal(old.server_world_age_at_last_sync, 1000)
  old.self.position.x = 999
  assert.equal(state().self.position.x, 0)
  at(300)
  assert.equal(state().tick, 2)
  assert.equal(state().self.position.x, 10)
  assert.equal(state().closest_entity.id, 3)
})

test('regular sampling exposes the newest eligible tick without polling', () => {
  const { bot, state, at } = fixture()
  for (let t = 0; t <= 10000; t += 50) {
    at(t)
    bot.emit('physicsTick')
  }
  assert.equal(state().tick, 196)
  assert.equal(state().age_ms, 250)
  at(10049)
  assert.equal(state().age_ms, 299)
})

test('no eligible entity is represented as null', () => {
  const { bot, state, at } = fixture()
  bot.entities = [bot.entity, { ...bot.entities[1], isValid: false }]
  bot.emit('physicsTick')
  at(250)
  assert.equal(state().closest_entity, null)
})

test('actions replace held keys, including opposite directions and stop', async () => {
  const { bot, action } = fixture()
  await action({ forward: 1, strafe: -1, jump: true, sprint: true })
  assert.deepEqual(bot.controls, {
    forward: true, back: false, left: true, right: false, jump: true, sprint: true
  })
  await action({ forward: -1, strafe: 1 })
  assert.deepEqual(bot.controls, {
    forward: false, back: true, left: false, right: true, jump: false, sprint: false
  })
  await action({})
  assert.ok(Object.values(bot.controls).every(value => value === false))
})

test('aim applies before one crosshair attack; a miss swings without autoaim', async () => {
  const { bot, action, state } = fixture()
  bot.target = bot.entities[1]
  await action({ delta_yaw: 0.02, delta_pitch: 10, attack: true })
  assert.deepEqual(bot.calls, [
    ['look', 0.02, Math.PI / 2, true], ['cursor', 3], ['attack', 2]
  ])
  assert.equal(state(), null) // Actions cannot publish an early observation.
  bot.calls = []
  bot.target = null
  await action({ attack: true })
  assert.deepEqual(bot.calls, [['cursor', 3], ['swing']])
  bot.calls = []
  await action({ delta_pitch: -10 })
  assert.equal(bot.entity.pitch, -Math.PI / 2)
  assert.equal(bot.calls.length, 1)
})

test('invalid actions fail before changing controls', async () => {
  const { bot, action } = fixture()
  await action({ forward: 1 })
  const before = { ...bot.controls }
  for (const invalid of [{ forward: 2 }, { strafe: 0.2 }, { sprint: 1 }, { delta_yaw: null }]) {
    await assert.rejects(action(invalid))
    assert.deepEqual(bot.controls, before)
  }
})

for (const event of ['death', 'respawn', 'end']) {
  test(`${event} clears keys and old observations; spawn requires warmup again`, async () => {
    const { bot, controller, action, state, at } = fixture()
    bot.emit('physicsTick')
    at(250)
    assert.ok(state())
    await action({ forward: 1 })
    bot.emit(event)
    assert.equal(state(), null)
    assert.deepEqual(bot.controls, {})
    await assert.rejects(action({}))
    bot.emit('spawn')
    bot.emit('physicsTick')
    at(499)
    assert.equal(state(), null)
    at(500)
    assert.equal(state().tick, 2)
    controller.close()
    assert.equal(state(), null)
    assert.equal(bot.listenerCount('physicsTick'), 0)
    assert.equal(bot.listenerCount('spawn'), 0)
    await assert.rejects(action({}))
  })
}

test('delay is configurable and validated', () => {
  for (const delay of [200, 300, 0]) {
    const { bot, state, at } = fixture(delay)
    bot.emit('physicsTick')
    if (delay) {
      at(delay - 1)
      assert.equal(state(), null)
    }
    at(delay)
    assert.equal(state().age_ms, delay)
  }
  for (const delay of [-1, NaN, Infinity]) assert.throws(() => fixture(delay))
})

test('episode barrier clears delayed state and restricts selection to assigned opponent', async () => {
  const { bot, controller, state, action, at } = fixture()
  bot.emit('physicsTick')
  at(250)
  assert.ok(state())
  controller.prepareEpisodeJSON(JSON.stringify({ episode_id: 42, opponent_username: 'player3' }))
  assert.equal(state(), null)
  await assert.rejects(action({ forward: 1 }))
  bot.emit('physicsTick')
  controller.resumeEpisode()
  bot.emit('physicsTick')
  at(499)
  assert.equal(state(), null)
  at(500)
  assert.equal(state().episode_id, 42)
  assert.equal(state().closest_entity.username, 'player3')
  assert.equal(JSON.parse(controller.getLifecycleJSON()).teleport_count, 0)
  bot.emit('forcedMove')
  assert.equal(JSON.parse(controller.getLifecycleJSON()).teleport_count, 1)
})

test('an awaited aim from an old episode cannot attack after the next episode starts', async () => {
  const { bot, controller, action } = fixture()
  let completeLook
  bot.look = () => new Promise(resolve => { completeLook = resolve })
  const pending = action({ delta_yaw: 0.1, attack: true })
  controller.prepareEpisodeJSON(JSON.stringify({ episode_id: 2, opponent_username: 'player2' }))
  controller.resumeEpisode()
  completeLook()
  await pending
  assert.deepEqual(bot.calls, [])
  assert.deepEqual(bot.controls, {})
})

test('a spawn during reset stays paused until explicitly resumed', async () => {
  const { bot, controller, state, action } = fixture()
  controller.prepareEpisodeJSON(JSON.stringify({ episode_id: 2, opponent_username: 'player2' }))
  bot.emit('spawn')
  assert.equal(JSON.parse(controller.getLifecycleJSON()).spawned, true)
  await assert.rejects(action({ attack: true }))
  bot.emit('physicsTick')
  assert.equal(state(), null)
})

const center = { x: 0, y: 64, z: 0 }
const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-10,
  `${actual} should be near ${expected}`)

test('polar positions, velocities and relative aim use the documented frames', () => {
  const { bot, state } = fixture(0, { arenaCenter: center })
  bot.entity.position = { x: 0, y: 66, z: -4 }
  bot.entity.yaw = Math.PI / 2
  bot.entity.pitch = 0.2
  bot.entities[1].position = { x: -3, y: 67, z: -4 }
  bot.entities[1].headYaw = -Math.PI / 2
  bot.entities[1].pitch = -Math.atan2(1, 3)
  bot.emit('physicsTick')
  const obs = state().observation
  assert.deepEqual(obs.self_position, { radius: 4, yaw: 0, height: 2 })
  near(obs.self_aim_to_center.yaw, -Math.PI / 2)
  near(obs.self_aim_to_center.pitch, 0.2 + Math.atan2(2, 4))
  assert.deepEqual(obs.opponent_position, { radius: 3, yaw: 0, height: 1 })
  near(obs.opponent_aim_to_self.yaw, 0)
  near(obs.opponent_aim_to_self.pitch, 0)
  assert.equal(obs.on_ground, true)
  assert.equal(obs.self_velocity, null)
  assert.equal(obs.opponent_velocity, null)

  // Current center ray is -Z. Self moved up and inward.
  bot.entity.position = { x: 0, y: 67, z: -3 }
  bot.emit('physicsTick')
  const second = state().observation
  near(second.self_velocity.radial, -1)
  near(second.self_velocity.tangential, 0)
  near(second.self_velocity.vertical, 1)
  // Stationary opponent moves relative to the moving player.
  const r = Math.sqrt(10)
  near(second.opponent_velocity.radial, 1 / r)
  near(second.opponent_velocity.tangential, -3 / r)
  near(second.opponent_velocity.vertical, -1)
})

test('tangential velocity is positive in the direction of increasing polar yaw', () => {
  const { bot, state } = fixture(0, { arenaCenter: center })
  bot.entity.position = { x: 1, y: 64, z: -4 }
  bot.emit('physicsTick')
  bot.entity.position.x = 0
  bot.emit('physicsTick')
  near(state().observation.self_velocity.radial, 0)
  near(state().observation.self_velocity.tangential, 1)
})

test('velocity history samples every tick even when state is never polled', () => {
  const { bot, state, at } = fixture(250, { arenaCenter: center })
  for (let t = 0; t <= 3000; t += 50) {
    at(t)
    bot.entity.position.x = t / 50
    bot.emit('physicsTick')
  }
  const obs = state().observation
  near(obs.self_position.radius, 55)
  near(obs.self_velocity.radial, 1)
  assert.deepEqual(state().observation, obs)
})

test('missing or replaced opponents invalidate velocity rather than bridging their poses', () => {
  const { bot, state } = fixture(0, { arenaCenter: center })
  bot.emit('physicsTick')
  bot.emit('physicsTick')
  assert.ok(state().observation.opponent_velocity)
  bot.entities[1].isValid = false
  bot.emit('physicsTick')
  assert.equal(state().closest_entity.id, 3)
  assert.equal(state().observation.opponent_velocity, null)
  bot.entities[2].type = 'mob'
  bot.emit('physicsTick')
  const obs = state().observation
  assert.equal(obs.opponent_position, null)
  assert.equal(obs.opponent_velocity, null)
  assert.equal(obs.opponent_aim_to_self, null)
  bot.entities[1].isValid = true
  bot.emit('physicsTick')
  assert.equal(state().observation.opponent_velocity, null)
})

test('hurt timers and ground state obey the snapshot delay and do not count attack clicks', async () => {
  const { bot, state, action, at } = fixture(250, { arenaCenter: center })
  bot.emit('physicsTick')
  at(50)
  bot.target = bot.entities[1]
  await action({ attack: true })
  bot.emit('physicsTick')
  at(100)
  bot.emit('entityHurt', bot.entity)
  bot.emit('entityHurt', bot.entities[1])
  bot.entity.onGround = false
  bot.emit('physicsTick')
  at(250)
  assert.equal(state().observation.ticks_since_gotten_hit, null)
  assert.equal(state().observation.on_ground, true)
  at(300)
  assert.equal(state().observation.ticks_since_hit_on_opponent, null)
  at(350)
  assert.equal(state().observation.ticks_since_gotten_hit, 0)
  assert.equal(state().observation.ticks_since_hit_on_opponent, 0)
  assert.equal(state().observation.on_ground, false)
  bot.emit('physicsTick')
  at(600)
  assert.equal(state().observation.ticks_since_gotten_hit, 1)
  assert.equal(state().observation.ticks_since_hit_on_opponent, 1)
})

test('unrelated damage and expired attack attempts do not count as outgoing hits', async () => {
  const { bot, state, action } = fixture(0, { arenaCenter: center })
  bot.emit('entityHurt', bot.entities[1])
  bot.emit('physicsTick')
  assert.equal(state().observation.ticks_since_hit_on_opponent, null)
  bot.target = bot.entities[1]
  await action({ attack: true })
  bot.emit('entityHurt', bot.entities[2])
  bot.emit('physicsTick')
  assert.equal(state().observation.ticks_since_hit_on_opponent, null)
  for (let i = 0; i < 7; i++) bot.emit('physicsTick')
  bot.emit('entityHurt', bot.entities[1])
  bot.emit('physicsTick')
  assert.equal(state().observation.ticks_since_hit_on_opponent, null)
})

test('teleports and episode transitions clear velocity and hit history', async () => {
  const { bot, controller, state, action } = fixture(0, { arenaCenter: center })
  for (const reset of [() => bot.emit('forcedMove'), () => {
    controller.prepareEpisodeJSON(JSON.stringify({ episode_id: 2, opponent_username: 'player2' }))
    controller.resumeEpisode()
  }, () => { bot.emit('death'); bot.emit('spawn') }]) {
    bot.entity.position.x = 0
    bot.emit('physicsTick')
    bot.target = bot.entities[1]
    await action({ attack: true })
    bot.emit('entityHurt', bot.entity)
    bot.emit('entityHurt', bot.entities[1])
    bot.emit('physicsTick')
    assert.equal(state().observation.ticks_since_hit_on_opponent, 0)
    reset()
    assert.equal(state(), null)
    bot.entity.position.x = 100
    bot.emit('physicsTick')
    const obs = state().observation
    assert.equal(obs.self_velocity, null)
    assert.equal(obs.opponent_velocity, null)
    assert.equal(obs.ticks_since_gotten_hit, null)
    assert.equal(obs.ticks_since_hit_on_opponent, null)
  }
  controller.close()
  assert.equal(bot.listenerCount('entityHurt'), 0)
})

test('zero-radius geometry is finite, and arena center is validated', () => {
  const { bot, state } = fixture(0, { arenaCenter: center })
  bot.entities[1].position = { ...bot.entity.position }
  bot.emit('physicsTick')
  assert.deepEqual(state().observation.self_position, { radius: 0, yaw: 0, height: 0 })
  assert.deepEqual(state().observation.opponent_position, { radius: 0, yaw: 0, height: 0 })
  assert.equal(state().observation.self_aim_to_center.pitch, 0)
  for (const invalid of [{}, { x: 0, y: 0, z: NaN }, { x: 0, y: Infinity, z: 0 }]) {
    assert.throws(() => fixture(0, { arenaCenter: invalid }), /finite/)
  }
})

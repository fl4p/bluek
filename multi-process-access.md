# Multi-process adapter access with bluek

## Can multiple processes using bluek access the same adapter?

Short answer: **yes, with one hard limit — one ATT connection per peer, host-wide.**

bluek opens per-process sockets the kernel multiplexes, so adapter-sharing is
essentially the same story as "bluek coexists with bluetoothd":

- **Scanning** (`_mgmt.py:133`): each process gets its own `HCI_CHANNEL_CONTROL`
  socket. The kernel refcounts discovery and broadcasts MGMT events to all
  listeners. N bluek processes scanning at once is fine.
- **Connecting to different peers**: each L2CAP socket (`_l2cap.py:80`) is a
  per-fd ACL+ATT channel to one BD_ADDR. Two processes connecting to two
  different devices on the same `hci0` is fine — kernel multiplexes the radio.
- **Pre-scan inside connect** (the ≤2s mgmt discovery): refcounted by the
  kernel, so concurrent connects from multiple bluek processes don't fight.
- **Pairing**: delegated to `bluetoothctl`; keys live in the kernel/bluetoothd
  and are shared across all processes.

The limit: **ATT CID `0x0004` is a singleton per ACL link.** Two bluek processes
cannot both hold a GATT connection to the *same* peer at the same time — the
second `connect()` on CID 0x0004 to an already-connected peer will fail. Same
constraint applies host-wide: bluek vs. bluetoothd vs. another bluek all
compete for that one ATT channel per device. (CCCDs/notification subscriptions
live on the peer side and are tied to that single ATT bearer, which is part of
why BLE works this way.)

So: multi-process scanning + multi-process GATT to distinct peers = supported.
Multi-process GATT to the same peer = not supported by the stack, not a bluek
limitation.

## Would multi-process GATT to the same peer work with Bleak?

Yes — that's the actual difference, and it's the whole reason `bluetoothd`
exists.

**Bleak on Linux → BlueZ D-Bus**: multiple processes can all hold
`BleakClient`s to the same peer concurrently. They aren't really making
separate ATT connections — `bluetoothd` owns the one ATT bearer to that
device, and every D-Bus client (process A, process B, ...) issues `ReadValue`
/ `WriteValue` / `StartNotify` against `org.bluez.GattCharacteristic1`
objects. bluetoothd serializes the actual ATT traffic on the single bearer.
Notifications fan out to all D-Bus subscribers that called `StartNotify`
(bluetoothd refcounts the CCCD write). It looks like shared access because it
*is* shared — through a mediator.

**bluek → raw L2CAP**: no mediator. Each process tries to open its own L2CAP
socket on CID 0x0004 to the peer. The kernel allows exactly one, so the
second `connect()` fails. There's nothing to fan notifications out to either,
because each socket *is* its own ATT bearer.

That's the trade-off taken by going D-Bus-less: lower overhead per op, but
you give up the multi-client multiplexing that bluetoothd was doing. For
batmon-ha's one-process-owns-the-device model that's fine; if you ever
wanted two HA add-ons (or HA + a side script) to both talk GATT to the same
peer, you'd be back to needing bluetoothd or building a bluek-side mediator
daemon.

### Gotchas with Bleak's "shared" model

Even with bluetoothd mediating:

- If process A holds the connection and process B calls
  `BleakClient.connect()`, BlueZ returns the existing connection immediately.
- But `disconnect()` from either side tears down the *shared* bearer — there's
  no per-client refcount on the connection itself, only on notifications.
- Shared access works, but cooperative shutdown is on you.

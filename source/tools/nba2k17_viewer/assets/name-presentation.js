'use strict';

// Exact-build presentation companion. Inline roster names remain terminated.
const PROFILE = 'nba2k17-shai-name-v1';
const SURNAME = 'Gilgeous-Alexander';
const PAGE_SIZE = 4096;
const TEXT_OFFSET = 512;
const GETTER_RVA = 0x9dd050;
const GETTER_BYTES = '40534883ec208b594885db7414e80eb38f008bd3488bc84883c4205be99fb08f00488bc14883c4205bc3';
const JERSEY_RVA = 0xa948d0;
const JERSEY_BYTES = '48895c2408488974241048897c2418554154415541564157488dac2400ffffff4881ec000200004c8bad50010000';
const GETTER_CALLERS = [0x5f08f9, 0x4580c1];
const JERSEY_CALLERS = [0xa951cb, 0xa9520e, 0xa95250];

// No JavaScript callback runs on the game's hot name paths. Counters are fixed
// in size and atomic; they wrap at 32 bits rather than accumulating event logs.
const NATIVE_SOURCE = String.raw`
#include <glib.h>
#include <gum/guminterceptor.h>

extern volatile gint control[8];
extern const guint8 game_base[];
extern const guint16 fixed_name[];

static const guint16 first_name[] = { 'S', 'h', 'a', 'i', 0 };
static const guint16 compact_name[] = {
  'G', 'i', 'l', 'g', 'e', 'o', 'u', 's', 'A', 'l', 'e', 'x', 'a', 'n', 'd', 'e', 'r', 0
};

typedef struct { gboolean replace; } NameInvocation;

static gboolean equal_name(const guint16 * actual, const guint16 * expected,
                           guint capacity) {
  guint i;
  if (actual == NULL) return FALSE;
  for (i = 0; i != capacity; i++) {
    if (actual[i] != expected[i]) return FALSE;
    if (expected[i] == 0) return TRUE;
  }
  return FALSE;
}

static gboolean getter_caller(guintptr rva) {
  return rva == 0x5f08f9 || rva == 0x4580c1;
}

static gboolean jersey_caller(guintptr rva) {
  return rva == 0xa951cb || rva == 0xa9520e || rva == 0xa95250;
}

void set_enabled(gint value) {
  gint previous = g_atomic_int_add(&control[0], 0);
  g_atomic_int_add(&control[0], (value != 0) - previous);
}

gint read_counter(guint index) {
  return index < 8 ? g_atomic_int_add(&control[index], 0) : 0;
}

void getter_enter(GumInvocationContext * ic) {
  NameInvocation * call = GUM_IC_GET_INVOCATION_DATA(ic, NameInvocation);
  const guint8 * player;
  guintptr caller;
  call->replace = FALSE;
  if (!g_atomic_int_add(&control[0], 0)) return;
  caller = (guintptr) gum_invocation_context_get_return_address(ic) - (guintptr) game_base;
  if (!getter_caller(caller)) return;
  g_atomic_int_add(&control[1], 1);
  player = gum_invocation_context_get_nth_argument(ic, 0);
  if (player == NULL || *((const guint16 *) (player + 0x64)) != 7710) return;
  if (!equal_name((const guint16 *) (player + 0x24), first_name, 5)) return;
  if (!equal_name((const guint16 *) player, compact_name, 18)) return;
  call->replace = TRUE;
  g_atomic_int_add(&control[2], 1);
}

void getter_leave(GumInvocationContext * ic) {
  NameInvocation * call = GUM_IC_GET_INVOCATION_DATA(ic, NameInvocation);
  if (!call->replace || !g_atomic_int_add(&control[0], 0)) return;
  gum_invocation_context_replace_return_value(ic, (gpointer) fixed_name);
  g_atomic_int_add(&control[3], 1);
}

void jersey_enter(GumInvocationContext * ic) {
  const guint16 * text;
  guintptr caller;
  if (!g_atomic_int_add(&control[0], 0)) return;
  caller = (guintptr) gum_invocation_context_get_return_address(ic) - (guintptr) game_base;
  if (!jersey_caller(caller)) return;
  g_atomic_int_add(&control[4], 1);
  text = gum_invocation_context_get_nth_argument(ic, 0);
  if (!equal_name(text, compact_name, 18)) return;
  g_atomic_int_add(&control[5], 1);
  gum_invocation_context_replace_nth_argument(ic, 0, (gpointer) fixed_name);
  g_atomic_int_add(&control[6], 1);
}
`;

let state = 'new';
let game = null;
let token = null;
let retainedPage = null;
let reusedPage = false;
let nativeModule = null;
let controls = null;
let nativeEnable = null;
let nativeCounter = null;
const listeners = [];

function byteHex(address, size) {
  return Array.from(new Uint8Array(address.readByteArray(size)))
    .map(value => value.toString(16).padStart(2, '0')).join('');
}

function requireBytes(address, expected, label) {
  if (byteHex(address, expected.length / 2) !== expected)
    throw new Error(label + ' differs from the reviewed executable.');
}

function requireCalls(callers, targetRva, label) {
  for (const returnRva of callers) {
    const call = game.base.add(returnRva - 5);
    if (call.readU8() !== 0xe8 || call.add(1).readS32() + returnRva !== targetRva)
      throw new Error(label + ' call site differs from the reviewed executable.');
  }
}

function expectedHeader() {
  return JSON.stringify({magic: 'NBA2K17NamePresentation', version: 1,
    profile: PROFILE, pid: Process.id, module_base: game.base.toString(),
    process_token: token});
}

function verifyRetainedPage(address) {
  const range = Process.findRangeByAddress(address);
  if (range === null || range.protection !== 'r--' ||
      address.and(ptr(PAGE_SIZE - 1)).toString() !== '0x0' ||
      range.base.compare(address) > 0 ||
      range.base.add(range.size).compare(address.add(PAGE_SIZE)) < 0)
    throw new Error('Retained name page is not a valid read-only allocation.');
  const header = expectedHeader();
  if (address.readUtf8String(header.length) !== header ||
      address.add(header.length).readU8() !== 0)
    throw new Error('Retained name page belongs to another process or profile.');
  const expected = [];
  for (let i = 0; i < SURNAME.length; i++) {
    const code = SURNAME.charCodeAt(i);
    expected.push(code & 255, code >> 8);
  }
  expected.push(0, 0);
  if (byteHex(address.add(TEXT_OFFSET), expected.length) !==
      expected.map(value => value.toString(16).padStart(2, '0')).join(''))
    throw new Error('Retained surname text differs from the reviewed value.');
}

function snapshot() {
  const counters = nativeCounter === null ? Array(8).fill(0) :
    Array.from({length: 8}, (_, index) => nativeCounter(index) >>> 0);
  return {state, installed: state === 'attached', enabled: counters[0] === 1,
    pid: Process.id, process_token: token, profile: PROFILE,
    module_base: game === null ? null : game.base.toString(),
    retained_page: retainedPage === null ? null : retainedPage.toString(),
    reused_page: reusedPage, getter_candidates: counters[1],
    getter_matches: counters[2], getter_overrides: counters[3],
    jersey_candidates: counters[4], jersey_matches: counters[5],
    jersey_overrides: counters[6]};
}

function stop() {
  if (nativeEnable !== null) nativeEnable(0);
  while (listeners.length) listeners.pop().detach();
  Interceptor.flush();
  if (state !== 'new') state = 'stopped';
  return snapshot();
}

rpc.exports = {
  initialize(options) {
    if (state !== 'new') throw new Error('This companion instance has already initialized.');
    state = 'initializing';
    let unpublishedPage = false;
    try {
      if (Process.arch !== 'x64' || Process.platform !== 'windows')
        throw new Error('The name companion requires the reviewed Windows x64 build.');
      if (!options || typeof options.process_token !== 'string' ||
          !/^[0-9]{1,24}$/.test(options.process_token))
        throw new Error('A verified process creation token is required.');
      token = options.process_token;
      game = Process.getModuleByName('NBA2K17.exe');
      requireBytes(game.base.add(GETTER_RVA), GETTER_BYTES, 'Surname getter');
      requireBytes(game.base.add(JERSEY_RVA), JERSEY_BYTES, 'Jersey renderer');
      requireCalls(GETTER_CALLERS, GETTER_RVA, 'Surname getter');
      requireCalls(JERSEY_CALLERS, JERSEY_RVA, 'Jersey renderer');
      const cached = options.retained_page;
      if (cached !== undefined && cached !== null) {
        if (typeof cached !== 'string' || !/^0x[0-9a-f]{1,16}$/i.test(cached))
          throw new Error('Invalid retained name page address.');
        retainedPage = ptr(cached);
        verifyRetainedPage(retainedPage);
        reusedPage = true;
      } else {
        const allocate = new NativeFunction(Module.getGlobalExportByName('VirtualAlloc'),
          'pointer', ['pointer', 'size_t', 'uint32', 'uint32']);
        retainedPage = allocate(ptr(0), PAGE_SIZE, 0x3000, 0x04);
        if (retainedPage.isNull()) throw new Error('Cannot allocate stable name storage.');
        unpublishedPage = true;
        retainedPage.writeUtf8String(expectedHeader());
        retainedPage.add(TEXT_OFFSET).writeUtf16String(SURNAME);
        if (!Memory.protect(retainedPage, PAGE_SIZE, 'r--'))
          throw new Error('Cannot protect stable name storage.');
        verifyRetainedPage(retainedPage);
      }
      controls = Memory.alloc(32);
      controls.writeByteArray(new Uint8Array(32));
      nativeModule = new CModule(NATIVE_SOURCE, {
        control: controls, game_base: game.base, fixed_name: retainedPage.add(TEXT_OFFSET)
      }, {toolchain: 'internal'});
      nativeEnable = new NativeFunction(nativeModule.set_enabled, 'void', ['int']);
      nativeCounter = new NativeFunction(nativeModule.read_counter, 'int', ['uint']);
      listeners.push(Interceptor.attach(game.base.add(GETTER_RVA), {
        onEnter: nativeModule.getter_enter, onLeave: nativeModule.getter_leave
      }));
      listeners.push(Interceptor.attach(game.base.add(JERSEY_RVA), {
        onEnter: nativeModule.jersey_enter
      }));
      Interceptor.flush();
      state = 'attached';
      return snapshot();
    } catch (error) {
      if (nativeEnable !== null) nativeEnable(0);
      while (listeners.length) listeners.pop().detach();
      Interceptor.flush();
      // Initialization never enables the callbacks, so a new page has not been
      // returned to native callers and can be released after a failed setup.
      // A supplied existing page may already be cached by the game; retain it.
      if (unpublishedPage) {
        const release = new NativeFunction(Module.getGlobalExportByName('VirtualFree'),
          'bool', ['pointer', 'size_t', 'uint32']);
        if (release(retainedPage, 0, 0x8000)) retainedPage = null;
      }
      state = 'error';
      throw error;
    }
  },
  setenabled(enabled) {
    if (state !== 'attached' || typeof enabled !== 'boolean')
      throw new Error('The name companion is not attached or the setting is invalid.');
    nativeEnable(enabled ? 1 : 0);
    return snapshot();
  },
  status: snapshot,
  stop
};

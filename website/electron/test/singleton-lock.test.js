"use strict";

const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { parseLockTarget, describeSingletonLock } = require("../singleton-lock");

const HOST = "test-host.local";
const posixOnly = { skip: process.platform === "win32" };

function errno(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

const killDead = () => {
  throw errno("ESRCH");
};
const killAlive = () => true;
const killDenied = () => {
  throw errno("EPERM");
};

describe("parseLockTarget", () => {
  it("splits on the last dash so hostnames with dashes survive", () => {
    assert.deepEqual(parseLockTarget("my-mac-book.local-4242"), { host: "my-mac-book.local", pid: 4242 });
  });

  it("rejects targets without a numeric positive pid", () => {
    for (const target of ["", "nohyphen", "-123", "host-", "host-12a", "host-0", null, undefined]) {
      assert.equal(parseLockTarget(target), null, JSON.stringify(target));
    }
  });
});

describe("describeSingletonLock against a real userData dir", posixOnly, () => {
  let dir;

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "kirocrew-singleton-"));
  });

  afterEach(() => {
    fs.rmSync(dir, { recursive: true, force: true });
  });

  function describeWith(kill) {
    return describeSingletonLock({ userDataDir: dir, hostname: HOST, kill });
  }

  function writeLock(target) {
    fs.symlinkSync(target, path.join(dir, "SingletonLock"));
  }

  it("reports a missing lock", () => {
    assert.equal(describeWith(killAlive), "no SingletonLock file");
  });

  it("reports a same-host lock whose pid is gone", () => {
    writeLock(HOST + "-4242");
    assert.equal(
      describeWith(killDead),
      'SingletonLock target "test-host.local-4242", host matches, pid 4242 on this machine is not running',
    );
  });

  it("reports a same-host lock whose pid is running", () => {
    writeLock(HOST + "-4242");
    assert.match(describeWith(killAlive), /host matches, pid 4242 on this machine is running$/);
  });

  it("reports a host mismatch and the local probe result", () => {
    writeLock("old-name.local-4242");
    assert.equal(
      describeWith(killDead),
      'SingletonLock target "old-name.local-4242", host differs (this host is "test-host.local"), '
        + "pid 4242 on this machine is not running",
    );
  });

  it("reports a probe that could not decide", () => {
    writeLock(HOST + "-4242");
    assert.match(describeWith(killDenied), /pid 4242 on this machine could not be probed \(EPERM\)$/);
  });

  it("reports an unparseable target", () => {
    writeLock("garbage");
    assert.equal(describeWith(killAlive), 'SingletonLock target "garbage" is unparseable');
  });

  it("reports a lock that is a regular file", () => {
    fs.writeFileSync(path.join(dir, "SingletonLock"), "not a link");
    assert.equal(describeWith(killAlive), "SingletonLock is not a symlink");
  });

  it("never removes or changes the Singleton files", () => {
    writeLock(HOST + "-4242");
    fs.symlinkSync(path.join(dir, "gone", "SingletonSocket"), path.join(dir, "SingletonSocket"));
    describeWith(killDead);
    assert.equal(fs.readlinkSync(path.join(dir, "SingletonLock")), HOST + "-4242");
    assert.ok(fs.lstatSync(path.join(dir, "SingletonSocket")).isSymbolicLink());
  });
});

describe("describeSingletonLock failure handling", () => {
  it("reports an unreadable lock", () => {
    const fakeFs = {
      readlinkSync() {
        throw errno("EACCES");
      },
    };
    assert.equal(
      describeSingletonLock({ userDataDir: "/unreadable", fs: fakeFs, hostname: HOST, kill: killAlive }),
      "SingletonLock unreadable (EACCES)",
    );
  });

  it("never throws, even when its dependencies do", () => {
    const fakeFs = { readlinkSync: () => HOST + "-4242" };
    const kill = () => {
      throw new Error("boom");
    };
    assert.match(
      describeSingletonLock({ userDataDir: "/x", fs: fakeFs, hostname: HOST, kill }),
      /pid 4242 on this machine could not be probed \(Error: boom\)$/,
    );
    const badFs = { readlinkSync: () => ({ toString() { throw new Error("weird"); } }) };
    assert.doesNotThrow(() => describeSingletonLock({ userDataDir: "/x", fs: badFs, hostname: HOST, kill: killAlive }));
  });
});

const test = require("node:test");
const { reclaimStaleSingletonLock, isPidDead, SINGLETON_FILES } = require("../singleton-lock");

const MAIN_SRC = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");

const USER_DATA = path.join("fake", "userData");

function fakeFs(target) {
  const files = new Set(target === null ? [] : SINGLETON_FILES);
  return {
    files,
    readlinkSync(p) {
      if (p !== path.join(USER_DATA, "SingletonLock") || !files.has("SingletonLock")) {
        const error = new Error("ENOENT");
        error.code = "ENOENT";
        throw error;
      }
      return target;
    },
    unlinkSync(p) {
      const name = path.basename(p);
      assert.equal(path.dirname(p), USER_DATA);
      files.delete(name);
    },
  };
}

function killWith(code) {
  const calls = [];
  const kill = (pid, signal) => {
    calls.push([pid, signal]);
    if (code) {
      const error = new Error(code);
      error.code = code;
      throw error;
    }
  };
  kill.calls = calls;
  return kill;
}

function harness({ target, killCode, retryWins = true, platform = "darwin" }) {
  const logs = [];
  const state = { retries: 0 };
  const fsImpl = fakeFs(target);
  const kill = killWith(killCode);
  const app = {
    getPath: (name) => {
      assert.equal(name, "userData");
      return USER_DATA;
    },
    requestSingleInstanceLock: () => {
      state.retries += 1;
      return retryWins;
    },
  };
  const result = reclaimStaleSingletonLock({
    app,
    log: (l) => logs.push(l),
    fs: fsImpl,
    kill,
    platform,
  });
  return { result, logs, state, fsImpl, kill };
}

test("isPidDead is true only for ESRCH", () => {
  assert.equal(isPidDead(1, killWith("ESRCH")), true);
  assert.equal(isPidDead(1, killWith("EPERM")), false);
  assert.equal(isPidDead(1, killWith(null)), false);
});

test("isPidDead probes the real process table", () => {
  assert.equal(isPidDead(process.pid), false);
});

test("a dead owner's lock is cleared and the retry wins", () => {
  const { result, logs, state, fsImpl, kill } = harness({ target: "host-4242", killCode: "ESRCH" });
  assert.equal(result, true);
  assert.deepEqual(kill.calls, [[4242, 0]]);
  assert.equal(state.retries, 1);
  assert.equal(fsImpl.files.size, 0, "all three Singleton files are removed");
  assert.equal(logs.length, 1);
  assert.match(logs[0], /cleared stale lock 'host-4242'; retry won/);
});

test("a dead owner is reclaimed even when the hostname no longer matches", () => {
  const { result, fsImpl } = harness({ target: "some-other-host-4242", killCode: "ESRCH" });
  assert.equal(result, true);
  assert.equal(fsImpl.files.size, 0);
});

test("a live owner's lock is left alone and not retried", () => {
  const { result, logs, state, fsImpl } = harness({ target: "host-4242", killCode: null });
  assert.equal(result, false);
  assert.equal(state.retries, 0);
  assert.equal(fsImpl.files.size, SINGLETON_FILES.length);
  assert.match(logs[0], /names a running process; leaving it/);
});

test("EPERM counts as a live owner", () => {
  const { result, state, fsImpl } = harness({ target: "host-4242", killCode: "EPERM" });
  assert.equal(result, false);
  assert.equal(state.retries, 0);
  assert.equal(fsImpl.files.size, SINGLETON_FILES.length);
});

test("Windows never probes or deletes anything", () => {
  const { result, logs, state, fsImpl, kill } = harness({
    target: "host-4242",
    killCode: "ESRCH",
    platform: "win32",
  });
  assert.equal(result, false);
  assert.equal(state.retries, 0);
  assert.equal(kill.calls.length, 0, "process.kill(pid, 0) is not a liveness probe on Windows");
  assert.equal(fsImpl.files.size, SINGLETON_FILES.length);
  assert.deepEqual(logs, []);
});

test("no SingletonLock means there is nothing to reclaim", () => {
  const { result, logs, state, kill } = harness({ target: null, killCode: "ESRCH" });
  assert.equal(result, false);
  assert.equal(state.retries, 0);
  assert.equal(kill.calls.length, 0);
  assert.deepEqual(logs, []);
});

test("an unparseable lock target is left alone", () => {
  const { result, logs, state, fsImpl, kill } = harness({ target: "garbage", killCode: "ESRCH" });
  assert.equal(result, false);
  assert.equal(state.retries, 0);
  assert.equal(kill.calls.length, 0);
  assert.equal(fsImpl.files.size, SINGLETON_FILES.length);
  assert.match(logs[0], /is not <hostname>-<pid>/);
});

test("a retry that still loses returns false so the caller exits", () => {
  const { result, logs } = harness({ target: "host-4242", killCode: "ESRCH", retryWins: false });
  assert.equal(result, false);
  assert.match(logs[0], /retry lost/);
});

test("a throwing retry is logged and reported as lost", () => {
  const logs = [];
  const app = {
    getPath: () => USER_DATA,
    requestSingleInstanceLock: () => {
      throw new Error("boom");
    },
  };
  const result = reclaimStaleSingletonLock({
    app,
    log: (l) => logs.push(l),
    fs: fakeFs("host-4242"),
    kill: killWith("ESRCH"),
    platform: "linux",
  });
  assert.equal(result, false);
  assert.match(logs[0], /retry after clearing a stale lock failed: boom/);
});

test("an unavailable userData path fails closed without throwing", () => {
  const app = {
    getPath: () => {
      throw new Error("no userData");
    },
    requestSingleInstanceLock: () => assert.fail("must not retry"),
  };
  assert.equal(reclaimStaleSingletonLock({ app, log: () => {}, platform: "darwin" }), false);
});

test("real symlinks: a dead owner's files are removed from a temp userData", (t) => {
  if (process.platform === "win32") {
    t.skip("SingletonLock is a POSIX symlink");
    return;
  }
  const os = require("node:os");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "singleton-lock-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.symlinkSync("host-4242", path.join(dir, "SingletonLock"));
  fs.symlinkSync(path.join(dir, "missing-socket"), path.join(dir, "SingletonSocket"));
  fs.symlinkSync("12345", path.join(dir, "SingletonCookie"));
  const app = { getPath: () => dir, requestSingleInstanceLock: () => true };
  const result = reclaimStaleSingletonLock({ app, log: () => {}, kill: killWith("ESRCH") });
  assert.equal(result, true);
  assert.deepEqual(fs.readdirSync(dir), []);
});

test("main.js retries the lock through the reclaim before exiting", () => {
  assert.match(
    MAIN_SRC,
    /if \(!app\.requestSingleInstanceLock\(\) && !reclaimStaleSingletonLock\(\{ app, log: glog \}\)\) \{/,
  );
});

test("singleton-lock.js ships in the packaged app", () => {
  const files = require("../package.json").build.files;
  assert.ok(files.includes("singleton-lock.js"));
});

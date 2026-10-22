// Electron 外壳：只做两件事 —— 拉起 Python 内核、把内核的页面装进窗口。
// 界面与内核靠 127.0.0.1 上的 HTTP 通信，所以换壳不影响内核；
// 直接用浏览器打开同一个地址也能用（见内核启动时打印的链接）。

const { app, BrowserWindow, dialog, Menu, shell } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

// 打包后 __dirname 在 app.asar 内，源码运行时它是 frontend/
const ROOT = path.resolve(__dirname, '..');
const KERNEL_NAME = 'NetSwitcherKernel.exe';

let backend = null;
let win = null;
let quitting = false;

function kernelCandidates() {
  const list = [];
  if (app.isPackaged) list.push(path.join(process.resourcesPath, 'backend', KERNEL_NAME));
  list.push(path.join(ROOT, 'backend', KERNEL_NAME));
  list.push(path.join(ROOT, 'dist', 'kernel', 'NetSwitcherKernel', KERNEL_NAME));
  return list;
}

function resolveBackend() {
  for (const exe of kernelCandidates()) {
    if (fs.existsSync(exe)) {
      return { exe, args: ['serve', '--port', '0'], cwd: path.dirname(exe) };
    }
  }
  // 源码运行：用系统 Python
  for (const exe of [process.env.NETSWITCHER_PYTHON, 'python', 'py'].filter(Boolean)) {
    if (exe !== 'python' && exe !== 'py' && !fs.existsSync(exe)) continue;
    return { exe, args: ['-m', 'netswitcher', 'serve', '--port', '0'], cwd: ROOT };
  }
  return null;
}

function startBackend() {
  return new Promise((resolve, reject) => {
    const target = resolveBackend();
    if (!target) return reject(new Error('找不到内核：既没有打包好的 backend，也找不到 Python'));

    backend = spawn(target.exe, target.args, {
      cwd: target.cwd,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    });

    let settled = false;
    let buffer = '';
    const timer = setTimeout(() => fail(new Error('内核启动超时（25s）')), 25000);

    function fail(err) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        backend.kill();
      } catch (e) {
        /* 已经退了 */
      }
      reject(err);
    }

    backend.stdout.setEncoding('utf-8');
    backend.stdout.on('data', (chunk) => {
      buffer += chunk;
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || '';
      for (const line of lines) {
        const text = line.trim();
        if (!text.startsWith('{')) continue;
        try {
          const info = JSON.parse(text);
          if (info.type === 'ready' && !settled) {
            settled = true;
            clearTimeout(timer);
            resolve(info);
          }
        } catch (e) {
          /* 内核的普通日志行，忽略 */
        }
      }
    });

    backend.stderr.setEncoding('utf-8');
    backend.stderr.on('data', (chunk) => console.error('[backend]', String(chunk).trim()));

    backend.on('error', (err) => fail(err));
    backend.on('exit', (code) => {
      if (!settled) return fail(new Error(`内核退出（code ${code}）`));
      if (!quitting) {
        // 窗口还开着内核却没了 —— 说清楚，别让用户对着报错页面猜
        dialog.showErrorBox(
          '内核已退出',
          `后端进程结束（code ${code}）。\n\n界面将无法操作，请重新启动 NetSwitcher。\n日志：%APPDATA%\\NetSwitcher\\logs`
        );
      }
    });
  });
}

function createWindow(info) {
  const icon = process.resourcesPath && path.join(process.resourcesPath, 'icon.ico');
  win = new BrowserWindow({
    width: 1360,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    backgroundColor: '#191919',
    title: 'NetSwitcher',
    autoHideMenuBar: true,
    icon: icon && fs.existsSync(icon) ? icon : undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  Menu.setApplicationMenu(null);
  win.setMenuBarVisibility(false);

  const url = `http://127.0.0.1:${info.port}/?token=${encodeURIComponent(info.token)}`;
  let retries = 0;
  win.loadURL(url);

  // 端口刚 listen 时偶发白屏/空响应，给几次重试机会
  win.webContents.on('did-fail-load', () => {
    if (retries >= 5) return;
    retries += 1;
    setTimeout(() => win && win.loadURL(url), 600);
  });

  win.webContents.setWindowOpenHandler(({ url: target }) => {
    shell.openExternal(target);
    return { action: 'deny' };
  });

  win.on('closed', () => {
    win = null;
  });
}

app.setAppUserModelId('com.netswitcher.app');
// 界面就是表单和表格，用不上 GPU。关掉硬件加速能躲开一堆显卡驱动 / 远程桌面 / 虚拟机
// 下 GPU 进程反复崩溃的问题（那种崩溃是 FATAL，表现为窗口一闪就没了）。
app.disableHardwareAcceleration();
app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-gpu-compositing');

app.whenReady().then(async () => {
  try {
    const info = await startBackend();
    createWindow(info);
  } catch (err) {
    dialog.showErrorBox(
      '内核启动失败',
      `${err.message}\n\n可以先用命令行验证：\nNetSwitcherKernel.exe serve --open`
    );
    app.quit();
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      startBackend().then(createWindow).catch(() => {});
    }
  });
});

app.on('window-all-closed', () => app.quit());

app.on('before-quit', () => {
  quitting = true;
  if (backend && !backend.killed) {
    try {
      backend.kill();
    } catch (e) {
      /* 忽略 */
    }
  }
});

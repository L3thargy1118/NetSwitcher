// 只读地把少量信息交给页面，不暴露 Node 能力。
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('netswitcher', {
  shell: 'electron',
  platform: process.platform,
});

import { contextBridge, ipcRenderer } from 'electron'
import type { ClientEvent, DesktopApi, ServerApi } from '../shared/types'

const invoke = <T>(channel: string, ...args: unknown[]): Promise<T> => ipcRenderer.invoke(channel, ...args) as Promise<T>
const serverCall = <K extends keyof ServerApi>(method: K, ...args: Parameters<ServerApi[K]>): ReturnType<ServerApi[K]> =>
  invoke('server:call', method, args) as ReturnType<ServerApi[K]>
const server: ServerApi = {
  health: (...args) => serverCall('health', ...args),
  dictations: (...args) => serverCall('dictations', ...args),
  dictation: (...args) => serverCall('dictation', ...args),
  setFinal: (...args) => serverCall('setFinal', ...args),
  vocab: () => serverCall('vocab'),
  addVocab: (...args) => serverCall('addVocab', ...args),
  updateVocab: (...args) => serverCall('updateVocab', ...args),
  deleteVocab: (...args) => serverCall('deleteVocab', ...args),
  importVocab: (...args) => serverCall('importVocab', ...args),
  exportVocab: () => serverCall('exportVocab'),
  suppress: (...args) => serverCall('suppress', ...args),
  correction: (...args) => serverCall('correction', ...args),
  meetings: (...args) => serverCall('meetings', ...args),
  createMeeting: (...args) => serverCall('createMeeting', ...args),
  meeting: (...args) => serverCall('meeting', ...args),
  patchMeeting: (...args) => serverCall('patchMeeting', ...args),
  deleteMeeting: (...args) => serverCall('deleteMeeting', ...args),
  transcript: (...args) => serverCall('transcript', ...args),
  summarize: (...args) => serverCall('summarize', ...args),
  processMeeting: (...args) => serverCall('processMeeting', ...args)
}
const api: DesktopApi = {
  server,
  getSettings: () => invoke('settings:get'),
  saveSettings: settings => invoke('settings:save', settings),
  getState: () => invoke('state:get'),
  restartAgent: () => invoke('agent:restart'),
  listDevices: () => invoke('agent:devices'),
  inject: text => invoke('agent:inject', text),
  startMeeting: title => invoke('meeting:start', title),
  stopMeeting: () => invoke('meeting:stop'),
  correct: correction => invoke('correction:submit', correction),
  getSuggestion: () => invoke('suggestion:get'),
  resolveSuggestion: action => invoke('suggestion:resolve', action),
  overlayHold: hold => invoke('overlay:hold', hold),
  openMain: page => invoke('window:main', page),
  saveText: (name, text) => invoke('file:save', name, text),
  onEvent: listener => {
    const handler = (_event: Electron.IpcRendererEvent, event: ClientEvent) => listener(event)
    ipcRenderer.on('client:event', handler)
    return () => ipcRenderer.removeListener('client:event', handler)
  }
}
contextBridge.exposeInMainWorld('transcriptd', api)

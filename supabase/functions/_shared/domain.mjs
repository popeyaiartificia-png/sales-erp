export const CLOSED = ['won', 'lost', 'invalid'];
export const MASK = '\u2022'.repeat(8);
export const DEFAULT_CONFIG = {
  wa: {token: '', phoneId: '', version: 'v23.0'},
  email: {host: 'smtp.gmail.com', port: 465, user: '', password: '', fromName: ''},
  limits: {emailPerDay: 400, waPerDay: 200, gapSec: 20, windowStart: '09:30', windowEnd: '19:00', days: [0,1,2,3,4,5]},
  reminder: {enabled: false, time: '09:00', to: ''},
  optOut: [],
};
export const ready = cfg => ({wa: !!(cfg.wa.token && cfg.wa.phoneId), email: !!(cfg.email.user && cfg.email.password)});
export function config(saved = {}) {
  const cfg = structuredClone(DEFAULT_CONFIG);
  for (const key of ['wa', 'email', 'limits', 'reminder']) Object.assign(cfg[key], saved[key] || {});
  cfg.optOut = Array.isArray(saved.optOut) ? saved.optOut : [];
  return cfg;
}
export function masked(cfg) {
  const copy = structuredClone(cfg);
  for (const [section, key] of [['wa','token'],['email','password']]) {
    const value = copy[section][key];
    copy[section][key] = value ? MASK + value.slice(-4) : '';
  }
  return copy;
}
export function mergeConfig(previous, update) {
  const next = config(previous);
  for (const section of ['wa','email','limits','reminder']) {
    for (const key of Object.keys(DEFAULT_CONFIG[section])) {
      if (update[section]?.[key] === undefined) continue;
      const value = update[section][key];
      if (['token','password'].includes(key) && typeof value === 'string' && value.startsWith(MASK)) continue;
      next[section][key] = typeof value === 'string' ? value.trim() : value;
    }
  }
  const check = (ok, message) => {if (!ok) throw new Error(message)};
  for (const [section, keys] of [['wa',['token','phoneId','version']],['email',['host','user','password','fromName']],['reminder',['time','to']]]) {
    for (const key of keys) check(typeof next[section][key] === 'string' && next[section][key].length <= 10000, 'Invalid ' + section + ' setting');
  }
  check(/^v\d+\.\d+$/.test(next.wa.version), 'Enter a Graph API version such as v23.0');
  check(!next.wa.phoneId || /^\d+$/.test(next.wa.phoneId), 'WhatsApp phone ID must contain digits');
  check(/^[a-z0-9][a-z0-9.-]*$/i.test(next.email.host), 'Enter a valid SMTP hostname');
  check([465,2525,2587].includes(Number(next.email.port)), 'Cloud SMTP supports ports 465, 2525, or 2587. Use 465 for Gmail.');
  next.email.port = Number(next.email.port);
  for (const key of ['emailPerDay','waPerDay','gapSec']) {
    const n = Number(next.limits[key]);
    check(Number.isInteger(n) && n >= (key === 'gapSec' ? 3 : 0) && n <= 86400, 'Invalid sending limit');
    next.limits[key] = n;
  }
  const clock = value => typeof value === 'string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(value);
  check(clock(next.limits.windowStart) && clock(next.limits.windowEnd) && next.limits.windowStart < next.limits.windowEnd, 'Enter a valid sending window');
  check(clock(next.reminder.time) && typeof next.reminder.enabled === 'boolean', 'Invalid reminder settings');
  check(Array.isArray(next.limits.days) && next.limits.days.every(n => Number.isInteger(Number(n)) && Number(n) >= 0 && Number(n) <= 6), 'Invalid sending days');
  next.limits.days = [...new Set(next.limits.days.map(Number))];
  check(!/[\r\n]/.test(next.email.user + next.email.fromName + next.reminder.to), 'Email settings must not contain line breaks');
  if (update.optOut !== undefined) {
    check(Array.isArray(update.optOut) && update.optOut.length <= 10000 && update.optOut.every(x => typeof x === 'string' && x.length <= 320), 'Invalid opt-out list');
    next.optOut = update.optOut.map(x => x.trim()).filter(Boolean);
  }
  return next;
}
export function indiaTime(now = new Date()) {
  const local = new Date(now.getTime() + 330 * 60000);
  const date = local.toISOString().slice(0,10);
  return {date, day: (local.getUTCDay() + 6) % 7, minute: local.getUTCHours()*60 + local.getUTCMinutes(), start: Date.parse(date+'T00:00:00+05:30')/1000};
}
export const minute = clock => Number(clock.slice(0,2))*60 + Number(clock.slice(3,5));
export function inWindow(cfg, now = new Date()) {
  const time = indiaTime(now), limits = cfg.limits;
  return limits.days.includes(time.day) && time.minute >= minute(limits.windowStart) && time.minute < minute(limits.windowEnd);
}
export const firstOf = value => String(value || '').split(/\s*[,;]\s*/)[0].trim();
export const digits = value => String(value || '').replace(/\D/g, '');
export function phone(value, cc = '91') {
  let number = digits(firstOf(value));
  if (number.length === 11 && number.startsWith('0')) number = number.slice(1);
  if (number.length === 10) number = digits(cc || '91') + number;
  return number.length >= 10 ? number : '';
}
export function personFirst(lead) {
  let raw = String(lead.contact || '').split(/\s*[,;&]\s*|\s+and\s+/i)[0].replace(/\(.*?\)/g, '').trim();
  const honorific = raw.match(/^(mr|mrs|ms|miss|dr|shri|smt|er|prof|capt|col)\b\.?/i)?.[0] || '';
  const words = raw.slice(honorific.length).trim().split(/\s+/).filter(Boolean);
  if (!words.length) return lead.vcci ? '' : String(lead.name || '').split(/\s+/)[0];
  if (/^[a-z]\.?$/i.test(words[0])) return /^[a-z]\.?$/i.test(words.at(-1)) ? '' : (honorific ? honorific.replace(/\.$/,'')+'. ' : '') + words.at(-1);
  return words[0];
}
export function fill(text, lead, settings = {}) {
  const fields = {name: lead.name || '', firstName: personFirst(lead) || 'there', contact: lead.contact || '', company: lead.company || 'your business', city: lead.city || 'your city', industry: lead.industry || 'local', myName: settings.myName || '[your name]', myCompany: settings.myCompany || '[your company]'};
  return String(text || '').replace(/\{\{\s*(\w+)\s*\}\}/g, (_, key) => String(fields[key] ?? lead.extra?.[key] ?? ''));
}
export function optedOut(cfg, email = '', number = '') {
  return cfg.optOut.some(value => {
    const entry = value.trim().toLowerCase();
    return (email && entry === email.toLowerCase()) || (number && digits(entry).length >= 10 && digits(entry).slice(-10) === number.slice(-10));
  });
}
export function stopReason(seq, lead) {
  if (!seq) return 'Sequence was deleted';
  if (!lead) return 'Lead was deleted';
  return (seq.stopOn?.length ? seq.stopOn : CLOSED).includes(lead.status) ? 'Lead became "'+lead.status+'"' : '';
}

import { readFileSync } from 'node:fs';

const template = new URL('../workflows/monzoEmailAlerts.json.in', import.meta.url);
const workflow = JSON.parse(readFileSync(template, 'utf8'), (key, value) => {
  const match = typeof value === 'string' && /^\$\{(MONZO_ALERT_FROM|MONZO_ALERT_TO)\}$/.exec(value);
  if (!match) return value;

  const address = process.env[match[1]]?.trim();
  if (!address || !/^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/.test(address)) {
    throw new Error(`Set ${match[1]} to one email address in .env.alerts`);
  }
  return address;
});

process.stdout.write(JSON.stringify(workflow, null, 2) + '\n');

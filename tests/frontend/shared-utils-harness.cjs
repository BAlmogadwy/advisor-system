/* Evaluates the real static/js/shared-utils.js, as base.html loads it on every
   page, in a fresh vm context, and returns its helpers. A suite that runs a
   page slice without the whole page can inject these instead of a stub. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SHARED_UTILS = path.join(__dirname, '..', '..', 'static', 'js', 'shared-utils.js');

function loadSharedUtils() {
  const context = vm.createContext({ document: { cookie: '' } });
  vm.runInContext(fs.readFileSync(SHARED_UTILS, 'utf8'), context, { filename: 'shared-utils.js' });
  // Read the globals as page code does (by name), not as context properties.
  return vm.runInContext('({ safeHtml, esc, pluralAr })', context);
}

module.exports = { loadSharedUtils, SHARED_UTILS };

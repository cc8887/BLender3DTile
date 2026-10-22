const fs = require('node:fs');
const path = require('node:path');
const validator = require(path.join(__dirname, '..', '.tools', 'gltf-validator', 'node_modules', 'gltf-validator'));
const filename = process.argv[2];
validator.validateBytes(new Uint8Array(fs.readFileSync(filename)), {
  uri: path.basename(filename),
  maxIssues: 1000,
  externalResourceFunction: () => Promise.reject(new Error('External resources are forbidden'))
}).then(report => process.stdout.write(JSON.stringify(report)))
  .catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });

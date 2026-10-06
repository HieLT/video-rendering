// Pure configuration tests; no browser accounts or network.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const manifest = JSON.parse(fs.readFileSync('extensions/dola30/manifest.json', 'utf8'));
const source = fs.readFileSync('extensions/dola30/' + manifest.background.service_worker, 'utf8');
const start = source.indexOf('function patchActionBarDuration(');
const end = source.indexOf('function findImageOriRawUrls(', start);
const ctx = vm.createContext({console});
vm.runInContext(source.slice(start, end), ctx);
const patch = value => JSON.parse(ctx.patchActionBarDuration(JSON.stringify(value)));
const old = {key:'video-duration', option_list:[{option_key:'5'}, {option_key:'10'}]};
const legacy = patch(old);
assert.equal(legacy.option_list[2].option_key, '30');
assert.deepEqual(patch(legacy), legacy);
const template = {key:'video-duration', lower_bound:4, upper_bound:15, step_length:1, default_selected:10};
const modern = patch(template);
assert.deepEqual(modern, {...template, upper_bound:30});
assert.deepEqual(patch(modern), modern);
const nested = patch({data:{selectors:[{template:JSON.stringify(template)}]}});
assert.equal(JSON.parse(nested.data.selectors[0].template).upper_bound, 30);
const nestedTwice = patch({config: JSON.stringify({template:JSON.stringify(template)})});
assert.equal(JSON.parse(JSON.parse(nestedTwice.config).template).upper_bound, 30);
const capabilities = patch({model:{supported_duration_range:{lower:4, upper:15, step:1}}});
assert.equal(capabilities.model.supported_duration_range.upper, 30);
for (const unchanged of [
    {...template,key:'image-resolution'}, {...template,step_length:0},
    {...template,step_length:3}, {...template,upper_bound:60},
    {...template,upper_bound:'15'}, {key:'video-ratio', option_list:[{option_key:'16:9'}]}
]) assert.deepEqual(patch(unchanged), unchanged);
assert.equal(ctx.patchActionBarDuration('invalid json'), 'invalid json');
console.log('PASS legacy, slider templates, nested JSON, capability ranges, idempotence, unrelated/invalid ranges');

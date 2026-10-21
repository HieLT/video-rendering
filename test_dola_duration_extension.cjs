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
assert.deepEqual(legacy.option_list.map(option => option.option_key), ['5', '10', '15']);
assert.deepEqual(patch(legacy), legacy);
const underscored = patch({key:'video_duration',option_list:[
    {option_key:'5',display_text:'5s'},{option_key:'10',display_text:'10s'}
]});
assert.deepEqual(underscored.option_list.map(option => option.option_key), ['5','10','15']);
const labeled = patch({key:'length',option_list:[
    {option_key:'5',display_text:'5s'},{option_key:'10',display_text:'10s'}
]});
assert.deepEqual(labeled.option_list.map(option => option.option_key), ['5','10','15']);
const template = {key:'video-duration', lower_bound:4, upper_bound:30, step_length:1, default_selected:10};
const modern = patch(template);
assert.deepEqual(modern, {...template, upper_bound:15});
assert.deepEqual(patch(modern), modern);
const nested = patch({data:{selectors:[{template:JSON.stringify(template)}]}});
assert.equal(JSON.parse(nested.data.selectors[0].template).upper_bound, 15);
const nestedTwice = patch({config: JSON.stringify({template:JSON.stringify(template)})});
assert.equal(JSON.parse(JSON.parse(nestedTwice.config).template).upper_bound, 15);
const capabilities = patch({model:{supported_duration_range:{lower:4, upper:15, step:1}}});
assert.equal(capabilities.model.supported_duration_range.upper, 15);
const oldRange = patch({key:'video-duration', lower_bound:4, upper_bound:60, step_length:1});
assert.equal(oldRange.upper_bound, 15);
const legacy30 = patch({key:'video-duration', option_list:[{option_key:'5'}, {option_key:'10'}, {option_key:'30'}]});
assert.deepEqual(legacy30.option_list.map(option => option.option_key), ['5', '10', '15']);
const modelCapabilities = patch({model_capability:{
    'seedance_v2.0':{supported_durations:['5','10','30']},
    'seedance_v2.5':{supported_durations:['5','10','30']}
}});
assert.deepEqual(modelCapabilities.model_capability['seedance_v2.0'].supported_durations, ['5','10','15']);
assert.deepEqual(modelCapabilities.model_capability['seedance_v2.5'].supported_durations, ['5','10']);
for (const unchanged of [
    {...template,key:'image-resolution'}, {...template,step_length:0},
    {...template,step_length:3},
    {...template,upper_bound:'15'}, {key:'video-ratio', option_list:[{option_key:'16:9'}]}
]) assert.deepEqual(patch(unchanged), unchanged);
assert.equal(ctx.patchActionBarDuration('invalid json'), 'invalid json');
for (const filename of ['dola-skill-pack-response.json', 'doubao-skill-pack-response.json']) {
    const raw = fs.readFileSync('extensions/dola30/' + filename);
    const config = JSON.parse(raw.toString('latin1'));
    const serialized = JSON.stringify(config);
    assert.match(serialized, /"show_name":"15s"/);
    assert.doesNotMatch(serialized, /"show_name":"30s"/);
    assert.doesNotMatch(serialized, /"supported_durations":\["5","10","15","30"\]/);
}
const dolaConfig = JSON.parse(fs.readFileSync('extensions/dola30/dola-skill-pack-response.json').toString('latin1'));
const videoMeta = dolaConfig.data.video_generation.meta;
const modelOptions = videoMeta.option_list.find(option => option.value === 'model').options;
assert.ok(modelOptions.some(option => option.value === 'seedance_v2.5'));
assert.equal(modelOptions.find(option => option.is_default)?.value, 'seedance_v2.0');
const durationOptions = videoMeta.option_list.find(option => option.value === 'duration').options;
const defaultDuration = durationOptions.find(option => option.is_default)?.value;
assert.equal(defaultDuration, '15');
assert.ok(videoMeta.model_capability['seedance_v2.0'].supported_durations.includes(defaultDuration),
    'default duration must be compatible with the default 2.0 model');
assert.ok(!videoMeta.model_capability['seedance_v2.5'].supported_durations.includes('30'));
assert.ok(!durationOptions.some(option => option.value === '30'));
console.log('PASS 2.5 model option, compatible 15s default, 15s-only duration options, slider caps, capabilities, idempotence');

// Language-agnostic cold-reach latch self-test. Mirrors the hook's state machine
// (no message parsing). Proves: cold fs reach fires; recall-first stays silent;
// once-per-turn; cooldown; non-fs tools ignored; language-irrelevant.
const cfg = {
  enforce: true, agentFilter: "analyst",
  recallTools: ["memory_search","memory_get","graph_search","session_search","semantic_search","docs_search","data_query"],
  fsTools: ["exec","read","grep","glob"], cooldownTurns: 3,
};
const _state = new Map();
function fp(s){let h=0;for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return String(h);}
function gate(sessionKey, toolName, msg){
  if(cfg.agentFilter && !sessionKey.includes(cfg.agentFilter)) return null;
  if(!toolName) return null;
  const turnId = fp(msg||sessionKey);
  let st=_state.get(sessionKey);
  if(!st||st.turnId!==turnId){const turnIndex=(st?.turnIndex??0)+1;st={turnId,turnIndex,recallDone:false,firedTurn:st?.firedTurn??-Infinity};_state.set(sessionKey,st);}
  if(cfg.recallTools.includes(toolName)){st.recallDone=true;return null;}
  if(!cfg.fsTools.includes(toolName)) return null;
  if(st.recallDone) return null;
  if(st.turnIndex-st.firedTurn<cfg.cooldownTurns) return null;
  const already=st.firedTurn===st.turnIndex;st.firedTurn=st.turnIndex;
  if(already) return null;
  if(!cfg.enforce) return null;
  return "BLOCK";
}
let pass=0,fail=0;
function chk(name,got,want){const ok=got===want;console.log((ok?"PASS":"FAIL").padEnd(5),name,`(got=${got} want=${want})`);ok?pass++:fail++;}

const SK="agent:analyst:telegram:direct:1083618205";
// 1. cold exec on fresh turn -> BLOCK (any language, msg content irrelevant)
_state.clear();
chk("cold exec (english)", gate(SK,"exec","fix that plugin"), "BLOCK");
_state.clear();
chk("cold exec (indonesian)", gate(SK,"exec","perbaiki plugin itu"), "BLOCK");
_state.clear();
chk("cold exec (chinese)", gate(SK,"exec","修复那个插件"), "BLOCK");
_state.clear();
chk("cold read (spanish)", gate(SK,"read","arregla ese plugin"), "BLOCK");
// 2. recall-first -> fs stays silent
_state.clear();
gate(SK,"memory_search","apa yang kita bahas");
chk("recall then exec = silent", gate(SK,"exec","apa yang kita bahas"), null);
// 3. once per turn: second cold exec same turn -> silent (already fired)
_state.clear();
gate(SK,"exec","x");
chk("2nd cold exec same turn = silent", gate(SK,"exec","x"), null);
// 4. non-fs tool never gated
_state.clear();
chk("web_search never gated", gate(SK,"web_search","btc price"), null);
// 5. cooldown: turn2 within 3 turns of a fire -> silent
_state.clear();
gate(SK,"exec","turn-a");          // fires turn1
gate(SK,"exec","turn-b");          // turn2, within cooldown
chk("cooldown suppresses turn2", gate(SK,"exec","turn-b2-distinct"), null);
// 6. agentFilter: other agent ignored
chk("other agent ignored", gate("agent:cs:telegram:x","exec","y"), null);
// 7. cooldown expiry: after 3 turns, fires again
_state.clear();
gate(SK,"exec","t1");  // fire @1
gate(SK,"noop","t2");  // t2 (noop non-fs, just advances turn)
gate(SK,"noop","t3");  // t3
chk("fires again after cooldown", gate(SK,"exec","t4"), "BLOCK");

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail?1:0);

(() => {
  // Local and file-based copies continue using the original Python/browser storage.
  if (location.hostname !== 'popeyaiartificia-png.github.io') return;
  const url='https://sbxtmkhbtbkajvqfbbnf.supabase.co';
  const publishable='sb_publishable_DyOHzXhP6P7VJuabpApz4A_YRFRIRl0';
  const cloud=window.SalesQuestCloud={};
  document.documentElement.classList.add('sq-cloud-pending');
  const style=document.createElement('style');
  style.textContent='html.sq-cloud-pending body>*:not(#sq-cloud-auth){visibility:hidden}#sq-cloud-auth{position:fixed;inset:0;z-index:10000;background:#0a0c18;color:#eceeff;display:grid;place-items:center;font:16px system-ui;padding:24px}#sq-cloud-auth .sq-box{width:min(380px,100%)}#sq-cloud-auth input,#sq-cloud-auth button{box-sizing:border-box;width:100%;padding:12px;margin-top:10px;border-radius:9px;font:inherit}#sq-cloud-auth input{background:#141833;color:#eceeff;border:1px solid #323a72}#sq-cloud-auth button{background:#7c5cff;color:white;border:0;cursor:pointer}#sq-cloud-auth .sq-error{color:#ff9c9c;min-height:24px}';
  document.head.append(style);
  let client;
  const overlay=()=>{
    document.getElementById('sq-cloud-auth')?.remove();
    document.documentElement.classList.add('sq-cloud-pending');
    const root=document.createElement('div');root.id='sq-cloud-auth';document.body.append(root);return root;
  };
  cloud.showFailure=message=>{
    const root=overlay();
    root.innerHTML='<div class="sq-box"><h1>SalesQuest</h1><p class="sq-error"></p><button id="sq-retry">Try again</button><button id="sq-switch">Use another account</button></div>';
    root.querySelector('.sq-error').textContent=message;
    root.querySelector('#sq-retry').onclick=()=>location.reload();
    root.querySelector('#sq-switch').onclick=async()=>{await client?.auth.signOut({scope:'local'});location.reload()};
  };
  cloud.ready=(async()=>{
    if (document.readyState==='loading') await new Promise(resolve=>document.addEventListener('DOMContentLoaded',resolve,{once:true}));
    try {
      await new Promise((resolve,reject)=>{
        const script=document.createElement('script');script.src='https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.119.0/dist/umd/supabase.js';
        script.onload=resolve;script.onerror=()=>reject(new Error('Could not load sign-in. Check your connection and try again.'));document.head.append(script);
      });
      client=window.supabase.createClient(url,publishable);
      const {data,error}=await client.auth.getSession();if(error) throw error;
      if (!data.session) {
        const root=overlay();
        root.innerHTML='<form class="sq-box"><h1>SalesQuest</h1><p>Sign in to your sales workspace.</p><label>Email<input name="email" type="email" autocomplete="username" required></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><button>Sign in</button><p class="sq-error" role="alert"></p></form>';
        await new Promise(resolve=>{
          root.querySelector('form').onsubmit=async event=>{
            event.preventDefault();const form=event.currentTarget, button=form.querySelector('button');button.disabled=true;
            const {error}=await client.auth.signInWithPassword({email:form.elements.email.value.trim(),password:form.elements.password.value});
            button.disabled=false;
            if(error) form.querySelector('.sq-error').textContent=error.message;else resolve();
          };
        });
      }
      document.getElementById('sq-cloud-auth')?.remove();document.documentElement.classList.remove('sq-cloud-pending');
    } catch(error) {cloud.showFailure(error.message || 'Sign-in could not start.');throw error}
  })();
  cloud.request=async(path,opt={})=>{
    const {data,error}=await client.auth.getSession();
    if(error || !data.session) throw new Error('Your session expired. Reload SalesQuest to sign in again.');
    return fetch(url+'/functions/v1/salesquest-api/'+path,{...opt,headers:{...opt.headers,apikey:publishable,Authorization:'Bearer '+data.session.access_token}});
  };
  document.addEventListener('click',async event=>{
    if(event.target.closest('#sq-cloud-signout')) {await client.auth.signOut({scope:'local'});location.reload()}
  });
})();

let sessionId = null;

  function showError(msg){
    const box = document.getElementById('errorBox');
    box.textContent = msg;
    box.style.display = 'block';
  }
  function hideError(){
    document.getElementById('errorBox').style.display = 'none';
  }
  function setLoading(btnId, loading){
    document.getElementById(btnId).classList.toggle('loading', loading);
    document.getElementById(btnId).disabled = loading;
  }
  function goStep(n){
    document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
    const map = {1:'step1', 2:'step2', 2.5:'step2b', 3:'step3'};
    document.getElementById(map[n]).classList.add('active');
    document.getElementById('seg1').style.width = n >= 1 ? '100%' : '0%';
    document.getElementById('seg2').style.width = n >= 2 ? '100%' : '0%';
    document.getElementById('seg3').style.width = n >= 3 ? '100%' : '0%';
    hideError();
  }

  async function sendCode(){
    hideError();
    const phone = document.getElementById('phone').value.trim();
    if(!phone){ showError('شماره تلفن را وارد کنید.'); return; }
    setLoading('btnSendCode', true);
    try{
      const res = await fetch('/api/send-code', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({phone})
      });
      const data = await res.json();
      if(!res.ok){ showError(data.error || 'خطای ناشناخته.'); return; }
      sessionId = data.session_id;
      goStep(2);
    }catch(e){ showError('اتصال برقرار نشد. دوباره امتحان کنید.'); }
    finally{ setLoading('btnSendCode', false); }
  }

  async function verifyCode(){
    hideError();
    const code = document.getElementById('code').value.trim();
    if(!code){ showError('کد تایید را وارد کنید.'); return; }
    setLoading('btnVerify', true);
    try{
      const res = await fetch('/api/verify', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({session_id: sessionId, code})
      });
      const data = await res.json();
      if(!res.ok){ showError(data.error || 'خطای ناشناخته.'); return; }
      if(data.need_password){ goStep(2.5); return; }
      finish(data.session_string);
    }catch(e){ showError('اتصال برقرار نشد. دوباره امتحان کنید.'); }
    finally{ setLoading('btnVerify', false); }
  }

  async function verifyPassword(){
    hideError();
    const password = document.getElementById('password').value;
    if(!password){ showError('رمز عبور را وارد کنید.'); return; }
    setLoading('btnPassword', true);
    try{
      const res = await fetch('/api/verify', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({session_id: sessionId, password})
      });
      const data = await res.json();
      if(!res.ok){ showError(data.error || 'خطای ناشناخته.'); return; }
      finish(data.session_string);
    }catch(e){ showError('اتصال برقرار نشد. دوباره امتحان کنید.'); }
    finally{ setLoading('btnPassword', false); }
  }

  function finish(sessionString){
    document.getElementById('sessionOutput').value = sessionString;
    goStep(3);
  }

  async function copyOutput(){
    const el = document.getElementById('sessionOutput');
    el.select();
    try{
      await navigator.clipboard.writeText(el.value);
    }catch(e){
      document.execCommand('copy');
    }
    const btn = document.getElementById('copyBtn');
    const label = document.getElementById('copyLabel');
    btn.classList.add('copied');
    label.textContent = 'کپی شد ✓';
    setTimeout(()=>{ btn.classList.remove('copied'); label.textContent = 'کپی کردن کد'; }, 1800);
  }

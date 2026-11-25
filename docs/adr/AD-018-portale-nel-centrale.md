# AD-018: Il portale dei clienti nel pannello centrale

Stato: **accettata da Carlo il 24/11/2026** (risposte prima di §11). La direzione l'ha decisa Carlo il 24/11/2026 (AD-017, «Le risposte di
Carlo», punto 2). Questo documento decide la forma. Le domande aperte sono in §10.

Il codice è stato letto alla revisione `d74373a` (`carlo_dev_2026`). Le misure vengono dal codice, da `git log` e dallo
stato del centrale di sviluppo (`~/omniacli-local`). Su questo stato ho contato le voci senza leggere i nomi. Le macchine
dei clienti **non** le ho interrogate: sono in sola lettura, e anche una lettura finisce nel loro registro. I loro numeri
sono la fetta 0. Dove un numero è stimato, lo dice.

Le regole di AD-017 valgono e qui non si ripetono. Due cambiano, e lo dice §18.6:
- i dati del cliente stanno sul disco del suo workspace (AD-002): resta vero per i dati delle istanze, ma le **identità**
  delle persone del cliente passano al centrale;
- ogni azione è un job nel registro della macchina che agisce: resta vero, e il centrale registra in più chi è entrato e
  da dove.

Origine: Carlo, 24/11/2026. Ha scelto il portale nel centrale, contro la raccomandata di AD-017 §10.2. Il modello che
vuole è quello dei pannelli SaaS di gestione Odoo: un pannello centrale con utenti, clienti e portale, e su ogni server
solo un agente che esegue i comandi. Il centrale andrà su un VPS esterno, gira da una release, con il secondo fattore
obbligatorio fuori da `localhost`. La sua riserva è il backup cifrato. Non ci sono `TS-NNN` né `T<n>`/`HT<n>`.

## 0. Il problema e il costo, in numeri

**Com'è oggi.** Ogni macchina è la casa delle persone dei suoi workspace. Ha il loro login, il secondo fattore, le
passkey, il single sign-on, i token e il suo portale: un nome solo per macchina, `portal.address` in `options.yaml`,
pubblicato da `workspace-helper portal`. Il centrale non ha utenti dei clienti.

| Cosa | Quanto | Fonte |
|---|---|---|
| Identità dei clienti sul centrale oggi | 0 utenti in `users.yaml` (più 1 token), 0 workspace, 0 passkey, 2 sessioni; non ci sono `second-factor.json`, `sso.json`, `oauth.json`, `invitations.yaml` | `~/omniacli-local`, solo conteggi |
| File d'identità su ogni macchina | 8, mescolati con quelli del personale della macchina (tabella di 18.1) | `config.py`, `app.py` righe 640, 776-793 |
| Codice dell'identità | 2.253 righe in 6 moduli: `auth` 170, `sessions` 246, `totp` 311, `passkeys` 308, `sso` 517, `oauth` 701. **Non si sposta**: è lo stesso pacchetto, e sul centrale c'è già | `wc -l` |
| Route raggiungibili da un cliente | 143 (`WORKSPACE_ROUTES`, `app.py` riga 380), di cui 28 non-API. 22 sono d'identità (account, secondo fattore, passkey, sessioni, SSO, membri, inviti, token, ruoli, revoca OAuth); 121 operano su server, job, backup e log | `app.py` |
| Pagine HTML viste da un cliente | 13 GET: 3 di classe A (`instances`, `backups`, log), 6 di classe B (`account`, `jobs`, `job`, `workspaces`, destinazioni, registro del workspace), 3 di classe C (`index`, `server`, `workspace`), più il frammento della Dashboard | classi di AD-017 §17.1 |
| Macchine dei clienti | 5, tutte collegate via SSH | `links.yaml`, solo tipo |
| Portali pubblicati sulle macchine dei clienti | **probabilmente 0**: l'unico pubblicato, il 21/11, su un nome `sslip.io`, è stato tolto su richiesta di Carlo. Da misurare | memoria del progetto, fetta 0 |
| Persone, passkey, secondi fattori, SSO, token e grant OAuth per macchina | **non misurati** | fetta 0 |
| Lavoro stimato | ≈ 20 giornate in 8 fette, più le fette di AD-017 da cui dipende (§9, 18.8) | stima |

Se il numero dei portali pubblicati è davvero 0, oggi nessun cliente ha una passkey, un SSO o un connettore OAuth legato a
un nome che cambierebbe: **è il momento più economico** per scegliere il nome definitivo.

---

## AD-018: La forma

**Contesto.**
- AD-017 mette pagine, testi e liste nel centrale, e dati, API, helper e autorizzazione sulla macchina. Il portale
  restava sulle macchine (17.4.1).
- Carlo lo vuole nel centrale.
- AD-014 ha già il meccanismo per servire le persone di un workspace da una macchina che non ha l'istanza: la **casa**
  manda la richiesta all'host con `X-OmniaCLI-Scope`. L'host la accetta solo dal collegamento che
  `hosted-workspaces.json` nomina `home` di quel workspace, con ruolo admin, e la ricontrolla (`app.py` righe 935-1000,
  `placement.py`).

**Opzioni considerate** (le scartate, con il motivo, sono in §11):
- A. **Il centrale è la casa (AD-014) di ogni workspace dei clienti.** Persone, secondo fattore, passkey, SSO, token,
  OAuth e inviti stanno nel centrale. Ogni macchina resta host dei suoi workspace: tiene server, dati, nomi, limiti,
  pianificazioni, CI e monitor, e accetta l'ambito solo dal collegamento del centrale.
- B. Identità nel centrale, ma copiata su ogni macchina, così che il login funzioni anche col centrale fermo.
- C. Il centrale come provider OIDC, e il portale che resta sulle macchine come relying party.
- D. Il centrale che inoltra ai clienti l'HTML scritto dalle macchine, come `/placed` oggi.

**Decisione.** A, in sette parti (18.1-18.7).

**Motivazione.**
- 少: niente meccanismo nuovo. L'intestazione d'ambito, i due controlli, `hosted-workspaces.json`, l'inoltro di REST e
  MCP con `kind: token` e le liste chieste con lo stesso ambito esistono e sono provati (AD-008 §9 passo 5). L'import
  degli utenti con hash e secondo fattore esiste già nella consegna (`app.py` riga 26978, `second_factors.adopt`,
  `handover.names_map`).
- 美: è la forma che Carlo descrive, un centrale con le persone e un agente che esegue, ed è quella che AD-014 aveva già
  disegnato per due macchine.
- 軽: la macchina continua a non chiamare mai il centrale. Il centrale fermo ferma il portale, non Odoo.

**Contratto d'invarianti** (le proprietà che il riuso di AD-014 presume):

| Invariante | Vale? |
|---|---|
| Il W dell'host esiste perché un server è stato spostato lì, e la casa ne tiene una copia ferma | **No**. Qui il W è nativo della macchina e il centrale non ha dati: niente copia ferma, niente «Torna». Il record di casa sulla macchina non deve implicare uno spostamento (vincolo 8.2) |
| Il collegamento della casa ha ruolo admin | **Sì**: il token del centrale è admin (AD-017 17.5.4), e l'ambito restringe soltanto |
| Un nome utente è unico | **Solo per macchina.** Su 5 macchine due persone possono avere lo stesso nome: serve la regola di 18.3.4 |
| Una passkey vale sul nome dove è nata | **Sì, alla lettera**: l'RP ID è l'`Host` esatto (`passkeys.rp_of`). Su un altro nome non vale. È il motivo di 18.4 |
| Il token di un cliente vale all'indirizzo che il suo client chiama | **Sì** per i token della pagina dei token (Bearer, `sha256$`: l'hash si sposta). **No** per i grant OAuth: il token è legato all'URL MCP (RFC 8707, `oauth.py`), e cambiando nome il connettore si autorizza di nuovo |
| La macchina non chiama mai la casa | **Sì**, e resta così: è la regola di AD-014 e di AD-017 |
| Una persona che ha già un secondo fattore non ne registra un altro con la sola password | **No sulla carta**. `has_second_factor` guarda solo le passkey del nome di adesso (`app.py` riga 1478): chi aveva solo una passkey sul nome vecchio, dopo la password riceve l'attivazione di un fattore nuovo. Vincolo 8.5 |

**Tracciabilità.** Chat di Carlo del 24/11; AD-017 §10.2 e «Le risposte di Carlo»; AD-014 (casa, ambito, due
controlli); AD-008…AD-013 (consegna).

### 18.1: Dove vive l'identità oggi, e cosa passa al centrale

| File sulla macchina | Cosa tiene | Al centrale? |
|---|---|---|
| `users.yaml` (`Access`) | hash PBKDF2-SHA256 da 600.000 iterazioni delle password; i ruoli (`workspace` per le persone dei clienti); i token (`sha256$`) con ruolo, workspace, CI e server; i collegamenti in entrata | Le persone con ruolo `workspace` e i token con un workspace **sì**. Il personale, i token della macchina e i collegamenti **no** |
| `workspaces.yaml` | per ogni W: server, storage, limiti, nomi, rinvii, `notify`, `relay`, **e** `members`, `roles`, `member_roles`, `member_servers`, `token_people` | Le cinque chiavi delle persone **sì**. Il resto **no**: è della macchina |
| `second-factor.json` | segreto TOTP (in chiaro, per forza: serve all'HMAC), salt, codici di recupero come hash, ultimo passo usato | **sì** |
| `passkeys.json` | per persona un handle casuale e, per ogni passkey, id, chiave pubblica COSE, contatore, **RP ID**, date | **sì**, ma vale solo sul suo RP ID (18.3) |
| `sso.json` | per ogni W il provider OIDC (issuer, client id, **client secret**, domini, ruolo, MFA che conta) e le identità (issuer + sub → persona) | **sì** |
| `oauth.json` | client registrati, grant, hash dei token, legati all'URL MCP | **sì**, con la riserva sul nome di 18.4 |
| `invitations.yaml` | hash del token del link, scadenza di 72 h, workspace | Gli inviti in attesa si **rifanno** dal centrale |
| `sessions.json` | hash del cookie, 8 h dal login, impronta della password | **no** (18.3.3) |

**Decisione.** Al centrale passano le persone dei workspace e i loro segreti. Sulla macchina restano:
- i suoi admin locali, cioè la riserva di AD-017 17.4.2;
- i token della macchina e il collegamento del centrale;
- tutto ciò che serve a far girare le istanze senza il centrale: `notify`, `relay`, i nomi, le pianificazioni, il
  segreto dei webhook.

La macchina riceve dal centrale solo quello che le serve per scrivere la pagina al cliente: `provider_label` e i nomi
dei membri, nel record di casa (`hosted-workspaces.json`), aggiornato con `revision` come la `sync` di AD-014. **Nessun
segreto delle persone torna sulla macchina.**

### 18.2: Il centrale casa di tutti

1. **Sul centrale** c'è un W per ogni workspace di un cliente. Ha persone, ruoli, token, SSO e la mappa dei suoi server
   verso il collegamento della macchina che li ha. Non ha dati né istanze.
2. **Sulla macchina** il W resta com'è, senza membri. Il suo record di casa nomina il collegamento del centrale.
   `delegated_scope` (`app.py` riga 960) funziona già così: guarda solo `ws`, `home` e il ruolo admin del collegamento,
   non lo stato di uno spostamento.
3. **Le richieste.** Ogni route di `WORKSPACE_ROUTES` su un server di W:
   - il centrale la controlla con il suo `in_workspaces`;
   - poi la manda alla macchina con l'ambito della persona;
   - la macchina la ricontrolla.

   Sono i due controlli di AD-014 §4.
4. **Le pagine.** Le scrive il centrale, con i modelli di pagina di AD-017 17.1 calcolati sulla macchina con il
   `Principal` dell'ambito. `denied`, `capabilities` e la redazione di `customerview` si calcolano sulla macchina.
   **Ai clienti il centrale non inoltra mai l'HTML di una macchina** (18.6).
5. **REST e MCP dei clienti** entrano dal centrale, che li manda alla macchina con `kind: token`. È AD-014 §6:
   `api_token` della macchina accetta un collegamento che porta l'ambito di un token. Per il resto un token legge
   soltanto.
6. **Le liste** (Dashboard, Istanze, Backup, job, campanella) mettono insieme le righe delle macchine di W, chieste con
   lo stesso ambito (5 s, `LIST_TIMEOUT`). Una macchina che non risponde si vede come «la macchina del fornitore non
   risponde».
7. **Un server spostato** (AD-015): il centrale lo manda **direttamente** all'host, perché legge `placements` della
   macchina di partenza. Non fa due salti. La macchina di partenza resta la sorgente dello spostamento, non la casa
   delle persone.

### 18.3: La migrazione, macchina per macchina, e cosa passa uguale

**18.3.1. Il giro, per una macchina M (tutti i suoi workspace insieme).**
1. **Prova a vuoto.** Il centrale chiede a M l'elenco, con un'API interna solo per il collegamento e in sola lettura.
   Per ogni W conta persone, secondi fattori, passkey per RP ID, SSO, token, grant, inviti in attesa e nomi già presi
   sul centrale. La pagina dice voce per voce «passa uguale» o «va rifatto, da chi».
2. **Congelamento.** Le modifiche d'identità di quei W su M si rifiutano con «Questo workspace passa a <portale>». Le
   azioni sulle istanze no.
3. **Esportazione e import.** Il pacchetto passa sul collegamento SSH. Il centrale importa con gli **stessi hash**:
   password, codici di recupero, token, grant. Importa anche i segreti TOTP con l'ultimo passo, le passkey con il loro
   contatore, le configurazioni SSO e le identità, e i ruoli personalizzati. È il codice dell'import della consegna,
   riusato.
4. **Il record di casa** su M diventa il collegamento del centrale (la `sync` di AD-014, con `revision`). Il login
   delle persone di quei W su M si spegne.
5. **Il nome** del portale passa al centrale (18.4).
6. **La copia su M** resta spenta e illeggibile dalle pagine per 7 giorni, come la copia ferma di AD-013. Poi i segreti
   delle persone si cancellano da M: segreto TOTP, client secret SSO, hash. Il job lo registra.

**18.3.2. Si annulla** con «Torna sulla macchina», entro i 7 giorni. È lo stesso import nel verso opposto: il centrale
esporta quello che è cambiato dalla `revision` del passaggio (password cambiate, fattori nuovi), M lo prende, il record
di casa si toglie, il login locale si riaccende e il nome torna. Dopo i 7 giorni tornare è una consegna completa.

**18.3.3. Cosa vede il cliente.**

| Cosa | Passa uguale? |
|---|---|
| Password | **Sì**: l'hash è portabile |
| App di autenticazione e codici di recupero | **Sì**: stesso segreto, stessi codici rimasti |
| Ruoli, server, ruoli personalizzati | **Sì** |
| Token REST/MCP della pagina dei token | **Sì, lo stesso token**: se il nome cambia (18.4) cambia solo l'indirizzo nel client |
| Configurazione SSO e identità già collegate | **Sì**. Se il nome cambia, l'admin del provider del cliente aggiunge l'URI di ritorno `https://<portale>/sso/<ws>/callback` |
| Connettori OAuth (assistenti AI) | **Sì** se il nome resta, perché il token è legato all'URL MCP. Altrimenti il connettore si autorizza di nuovo: un consenso, nessuna password nuova |
| Passkey | **Sì solo se il nome resta identico.** L'RP ID è l'`Host` esatto: la firma porta l'hash dell'RP ID, e il centrale su un altro nome la rifiuta; il browser non la offre nemmeno. Le Related Origin Requests di WebAuthn non le usiamo (vedi «Non verificato»). Un nome `sslip.io` contiene l'IP della macchina e **non può** puntare al centrale: le passkey nate lì si rifanno sempre |
| Sessioni | **No**: il cookie vale per nome di host e dura al massimo 8 h. Il cliente entra una volta, con la stessa password e lo stesso fattore |
| Inviti non ancora accettati | **No**: il link punta alla macchina. Si rimandano dal centrale |

**18.3.4. I nomi in collisione.** Vale la regola della consegna (Carlo, 23/11): un nome già preso arriva come
`<nome>.<workspace>`, poi `-2`, `-3`. È l'unico caso in cui il cliente cambia qualcosa: il suo nome di login. La prova a
vuoto li conta e il passaggio li dice, persona per persona (§10.5).

### 18.4: I nomi, il DNS, i certificati

**Decisione.**
1. Il centrale risponde su **due nomi propri**:
   - il **portale dei clienti** (`portale.<dominio>`);
   - il **pannello del personale**.

   La regola di oggi resta (`portal_admits`): sul nome del portale il personale viene rifiutato dopo la password giusta,
   con 403 nel registro. Vale anche al contrario: sul nome del personale i clienti vengono rifiutati.
2. **Un nome unico per tutti i clienti**, non uno per macchina né uno per cliente. La separazione vera è
   `in_workspaces`: lo dice già `docs/portale-clienti.md`, «Un nome per workspace: no, per ora».
3. **L'RP ID delle passkey del centrale è un'impostazione**: il dominio registrabile del portale, scelto una volta. Così
   il nome del portale può cambiare dentro lo stesso dominio senza perdere le passkey. Il centrale accetta solo le sue
   origini (`origins_of`): un sottodominio estraneo non può usare un'asserzione.
4. **Certificati**: Let's Encrypt sui due nomi, con l'helper di oggi (`workspace-helper portal`, `server_name` come
   lista). I nomi delle istanze Odoo **non cambiano**: restano sulle macchine, con i loro certificati.
5. **I portali già pubblicati** sulle macchine, se la fetta 0 ne trova:
   - un nome in un DNS nostro: punta al centrale, che lo serve come nome in più per 90 giorni. Le passkey nate lì
     continuano a valere, e ogni login propone di aggiungerne una sul nome nuovo. Poi un 301;
   - un nome `sslip.io` o nel DNS di altri: si cambia. Le passkey si rifanno con 18.3.3 e il vincolo 8.5.
6. **Gli indirizzi che OmniaCLI dà alle persone** (`public_base`: inviti, URL MCP, SSO, Aiuto) diventano quelli del
   portale del centrale. Sulla macchina `agent` il portale si toglie (`omniacli-_portal.conf`).

### 18.5: Il centrale fermo

**Continua, sulla macchina:**
- Odoo e i suoi database, con «Entra come» sul nome dell'istanza (`/.omniacli/login`);
- backup e pianificazioni, monitor e avvisi (`notify` è della macchina);
- i webhook della CI, con la decisione qui sotto.

**Si ferma:** il portale, REST e MCP dei clienti, gli inviti e i login SSO.

**Decisioni.**
1. **I webhook della CI non passano dal centrale.** Oggi, se il portale ha un indirizzo, un webhook di GitHub punta a
   `https://<portale>/hooks/github/<id>` (README, «The webhook made by itself»). Con il portale nel centrale passerebbe
   di lì. Nel passaggio di una macchina, il job riscrive i webhook che OmniaCLI ha fatto sul nome dell'istanza
   (`https://<istanza>/.omniacli/hooks/github/<id>`), con lo stesso id e lo stesso segreto, come fa oggi «riacceso».
   Solo un'istanza senza nome pubblico resta inoltrata dal centrale, con corpo e firma intatti come in AD-014, e la
   pagina dice che la sua CI dipende dal centrale.
2. **Una pagina di manutenzione statica** nel nginx del VPS (`error_page` su 502/503/504), in italiano e in inglese:
   «Il portale è momentaneamente fermo. I vostri siti Odoo funzionano normalmente.» Copre l'aggiornamento a mano (≈ 20 s,
   AD-017 §0) e il processo caduto. Non copre il VPS spento: per quello c'è §10.4.
3. **REST e MCP passano solo dal centrale.** Niente token validi in sola lettura sulla macchina (§10.3, raccomandata).
   Un token revocato sul centrale non può restare valido su una macchina che il centrale non raggiunge: la revoca è
   immediata solo se il token vive in un posto solo.
4. **Il monitor esterno** del centrale avvisa Carlo, ed è il monitor di AD-017 fetta 7.

### 18.6: Sicurezza e NIS 2

**Cosa tiene il centrale pubblico:**
- i 7 token dei collegamenti in chiaro e 15 file in `ssh/` (AD-017 17.5);
- in più, le persone di tutti i clienti: gli hash PBKDF2, i segreti TOTP in chiaro, i client secret SSO in chiaro, le
  chiavi pubbliche delle passkey, gli hash di token e grant.

**Decisioni.**
1. **Superficie.**
   - Il VPS espone solo 80 (ACME e 301) e 443. SSH è a chiave, dagli indirizzi del personale.
   - `/docs` e `openapi.json` restano spenti.
   - Senza sessione rispondono solo:
     - il login;
     - `/invite/<token>`;
     - `/sso/<ws>/callback`;
     - i documenti OAuth e MCP;
     - l'inoltro dei webhook delle istanze senza nome.
2. **Il secondo fattore è obbligatorio per tutti** (politica `everybody`): sul VPS non c'è `localhost`. Un SSO conta come
   secondo fattore solo se il W lo dichiara, come oggi.
3. **Limiti.**
   - Il `limit_req` di nginx sui POST di login e inviti (10 al minuto, raffica 5).
   - `LoginLimiter` per indirizzo **e** per nome: 5 errori in 15 minuti, poi attese da 30 s che raddoppiano fino a 15
     minuti.
   - REST: 600 letture e 60 scritture al minuto per token.

   Il limite per nome permette a un estraneo di ritardare di 15 minuti il login di un cliente di cui conosce il nome. È
   accettato: una passkey non ha quel nome.
4. **Segregazione per workspace.** La decide `in_workspaces` del centrale, e la macchina la ricontrolla. La macchina
   però non può accorgersi di un centrale che manda l'ambito di W2 per una persona di W1: per questo il test di parità
   (8.3) ha persone di due workspace su due macchine. Inoltre il centrale **non inoltra mai HTML di una macchina a un
   cliente**. Con `/placed` di oggi, una macchina compromessa scriverebbe script sotto l'origine del portale, e da lì
   potrebbe cambiare la password, aggiungere passkey e usare la sessione di ogni cliente che apre quelle pagine.
5. **I segreti a riposo**:
   - il backup notturno del centrale è cifrato (AD-017 17.4.3);
   - niente snapshot del provider in chiaro;
   - dopo i 7 giorni, sulla macchina non restano segreti delle persone.

   Non aggiungo una cifratura applicativa dei file: chi è root sul VPS leggerebbe anche la chiave.
6. **Registro accessi**, su due lati che nessuno dei due può riscrivere:
   - il centrale registra login, secondo fattore, passkey, sessioni, inviti, SSO, grant, ogni modifica d'identità e ogni
     richiesta inoltrata (persona, workspace, sessione o token);
   - la macchina registra ogni azione e ogni lettura classificata con il collegamento, `on_behalf` = la persona, e il
     workspace (AD-014 §5).

   Il registro del workspace che vede l'admin del cliente unisce i due lati in ordine di tempo. L'esportazione SIEM
   (`siem.py`) parte da tutti e due.
7. **Dati personali**: nomi, e-mail e indirizzi IP delle persone dei clienti stanno ora sul VPS. Il VPS deve stare
   nell'UE, e il registro dei trattamenti e il contratto con il cliente lo nominano.

### 18.7: Consegna e spostamento con l'identità nel centrale

**Consegna (AD-008…AD-013, passi 7-10).**
1. **L'indirizzo del codice** (`u`, con il pin del certificato) è oggi il portale della macchina. Diventa **un nome del
   workspace sulla macchina che ha i dati**, sotto `/.omniacli/handover/` come gli hook. I dati non passano dal VPS, e la
   consegna non dipende dal centrale fermo. Il centrale prepara e mostra, la macchina serve.
2. **Le persone.** Quando la consegna arriva a `ready` (il centrale lo legge da `handover_status`), il centrale:
   - spinge alla macchina il pacchetto d'identità di W, con la sua `revision`;
   - **congela** le modifiche d'identità di W fino al passaggio o all'annullamento.

   La macchina lo sigilla come oggi, per la chiave del cliente.
3. **Al commit** la macchina rifiuta subito l'ambito di W, e il centrale revoca i token di W quando legge lo stato. La
   revoca è effettiva nello stesso istante, perché ogni richiesta passa dalla macchina.
4. **Le identità di W sul centrale** si cancellano dopo i 7 giorni della copia ferma (AD-013).
5. **Il cliente che riceve** è `full`, con il suo portale (AD-017 17.3). Quello che non passa (`NOT_PASSED`: passkey,
   token, sessioni, inviti) non cambia.

**Spostamento (AD-014, AD-015).**
- Casa e host sono due macchine `agent`. Lo spostamento resta macchina-macchina: `placements.json`, `treesync`, il
  database.
- Sull'host, il record di casa di W nomina il **collegamento del centrale**: le persone arrivano dal centrale, non dalla
  macchina di partenza.
- Il centrale manda le richieste di un server spostato direttamente all'host (18.2.7).
- L'inoltro `/placed` della macchina di partenza resta per le macchine `full`, cioè chi ha ricevuto una consegna e sposta
  fra macchine sue.

### 18.8: L'ordine rispetto ad AD-017

- **Prima**: AD-017 fette 0 (firma) e 1 (contratto `agent_api`). Le fette 0-4 di AD-018 si costruiscono **in parallelo**
  alle fette 2-5 di AD-017: toccano identità e instradamento, non i modelli di pagina.
- **AD-017 fette 4 e 5 cambiano ordine**: prima le pagine che vede un cliente (5 B e 2 C, 18.0), poi le altre.
- **Il passaggio di una macchina vera (fetta 5)** aspetta:
  - le pagine dei clienti scritte dal centrale (AD-017 2-5), per il punto 4 di 18.6;
  - la modalità `agent` (AD-017 6);
  - il backup cifrato con il ripristino provato (AD-017 7): senza, perdere il VPS vuol dire perdere le identità di
    tutti i clienti;
  - il VPS (fetta 7 qui).
- **AD-017 fetta 8 (NAT)** va prima del passaggio dei clienti di una macchina dietro NAT.
- **Cosa sblocca**: AD-017 diceva «le fette 0-5 non toccano il portale». Resta vero fino al passaggio: le pagine dei
  clienti si fanno per il centrale, ma i clienti le vedono solo dopo il loro passaggio.

---

## 8. Vincoli per il dev-lead

1. Al centrale passano solo le persone con ruolo `workspace`, i token con un workspace e le cinque chiavi delle persone
   di `workspaces.yaml`. Personale, token della macchina e collegamenti restano. Un test controlla che dopo il passaggio
   la macchina non abbia più, per quei W, né segreti TOTP né client secret né hash di persone (dopo i 7 giorni).
2. Il record di casa di un W nativo non implica uno spostamento. «Torna», la copia ferma e `forward` non si applicano a
   un W la cui casa è il centrale. Il rifiuto dell'ambito da un altro collegamento resta un 403 nel registro.
3. **Parità.** Ogni route di `WORKSPACE_ROUTES` (143) per admin, developer, user e un ruolo personalizzato deve dare le
   stesse risposte in due casi:
   - la persona entra dal centrale;
   - la stessa persona entra sulla macchina prima del passaggio.

   Il risultato deve essere 0 differenze. In più, una persona di W1 non raggiunge nulla di W2, su un'altra macchina, da
   nessuna route.
4. Il centrale inoltra ai clienti solo JSON, download dichiarati e websocket. Non c'è `pages: machine` per una sessione
   di un cliente: una pagina senza modello dice «non disponibile».
5. **Nessuna attivazione di un secondo fattore con la sola password** per chi ne aveva uno sulla macchina. Chi l'ha perso
   nel passaggio (passkey su un RP ID non servito) lo rifà con un link monouso di 72 h, come un invito, mandato al
   passaggio. Il vincolo vale anche oggi fra il nome del pannello e quello del portale.
6. L'RP ID è un'impostazione del centrale. Le origini accettate restano solo le sue.
7. **REST e MCP insieme**: le API d'identità del centrale che hanno un gemello oggi (membri, token, revoca OAuth) lo
   tengono. L'export e l'import fra macchina e centrale sono interni, solo per il collegamento, come `/api/pages/*`.
8. **Registro**: ogni voce del centrale porta persona, workspace e sessione o token. Il registro del workspace unisce i
   due lati. `verify-access-log` vale sul centrale come sulla macchina.
9. **Tempi**: le liste 5 s e le pagine 60 s, come AD-017 8.4. Nessuna cache d'identità sulla macchina.
10. Niente nomi di macchine né del fornitore nelle pagine dei clienti (`customerview`, AD-017 8.7).

## 9. Piano a fette (ognuna si rilascia da sola e si annulla; lab3 = centrale, lab4 = macchina `agent` con due workspace)

Per ogni fetta: il rilascio, la prova su lab3/lab4, come si annulla, la stima. lab1 e lab2 restano di Carlo.

0. **Misura**, in sola lettura e con il sì di Carlo per ogni macchina. Per ognuna delle 5:
   - portale pubblicato sì/no e con che nome (DNS nostro, di altri, `sslip.io`);
   - per W: persone, secondi fattori, passkey per RP ID, SSO, token, grant, inviti, webhook GitHub sull'indirizzo del
     portale;
   - nomi in collisione fra le macchine.

   Con le GET di oggi attraverso il collegamento, dove bastano. Il resto arriva con la prova a vuoto della fetta 1.
   - *Stima*: 0,5 g.
1. **Esportazione, prova a vuoto e import** (18.3.1, passi 1-3), che riusano l'import della consegna.
   - *Prova*: lab4 con 2 W, 6 persone (TOTP, passkey, SSO finto, token, un grant) e 1 nome in collisione con lab3. La
     prova a vuoto conta giusto voce per voce. L'import su lab3 ha gli stessi hash. Il codice TOTP calcolato dal
     segreto entra su lab3. Nel JSON delle pagine ci sono 0 segreti.
   - *Si annulla*: import cancellato, lab4 non toccato.
   - *Stima*: 3 g.

   **Com'è nel codice (fetta 1).** `identityimport.py`, più il codice in `app.py` accanto alla protezione del centrale.
   - **Sulla macchina** due GET interne, `/api/identity/v1/inventory` (la prova a vuoto) e `/api/identity/v1/export`
     (il pacchetto). Rispondono solo a un collegamento con ruolo admin (dopo `X-OmniaCLI-Role-Cap`), mai a una persona
     (né qui né dalle pagine del collegamento: `person`), a un token, a un ambito delegato. Ogni lettura, anche
     rifiutata, va nel registro della macchina con `… -> <stato> for <persona>`. Il pacchetto non si scrive mai sulla
     macchina e risponde con `Cache-Control: no-store`. Il proxy `/links/<m>/…` del centrale rifiuta `api/identity/`
     con 404, come i modelli delle pagine.
   - **Cosa entra.** Per ogni W: le persone di ruolo `workspace` (vincolo 8.1), con l'hash, il record TOTP
     (`SecondFactors.record`: segreto, ultimo passo, sale, codici di recupero come hash), le passkey
     (`PasskeyStore.record`: handle, RP ID, contatore); le cinque chiavi delle persone di `workspaces.yaml`; i token di
     W con hash, ruolo, CI e server; SSO con client secret e identità (`SsoStore.identities_of`); i grant OAuth di W con
     i loro client. Gli inviti e i webhook si contano soltanto. Personale, token della macchina e collegamenti no.
   - **Sul centrale** `identity_act` (`status`, `import`, `delete`) serve la pagina `/identities/<link>` (da
     Collegamenti › Identità), `/api/v1/links/{name}/identities[/import|/delete]` e i tool MCP `identity_import_status`,
     `identity_import`, `identity_import_delete`. Solo admin del centrale, mai un collegamento, un token di W o di una CI;
     le scritture vogliono `confirm` = il nome del collegamento. `plan()` dice ogni voce `same`, `redo` (con chi: la
     persona, l'admin del provider, l'admin del workspace, il passaggio) o `stays`, contro il nome del portale del
     centrale (`portal_url()`): passkey e grant passano uguali solo se il loro nome è quello. Nessun segreto nella
     risposta: conteggi, nomi, verdetti.
   - **I nomi.** `<nome>.<workspace>` (il primo W della persona, per nome), poi `-2`, `-3`: `handover.names_map`,
     contro gli utenti del centrale **e** le persone importate da altri collegamenti.
   - **Dove sta.** `identity-imports/<link>.json` (600, cartella 700) accanto ai dati del centrale, nello slot
     `identity_imports` del backup notturno cifrato e nel purge. Il pacchetto resta solo in memoria: letto in streaming
     con al massimo 64 MiB (`MAX_PACKAGE`) e 60 s in tutto (`TIMEOUT`, `asyncio.wait_for`: i timeout di httpx sono per
     fase e una macchina che risponde un byte alla volta non li fa mai scattare), fuori da `identity_lock`, che tengono
     solo il controllo e il salvataggio: una macchina lenta non blocca un altro import né un annullamento. Rifare
     l'import sostituisce quello di prima; «Cancella l'import…» toglie il file e basta.
   - **La macchina non è fidata** (la regola di AD-009, la revisione della fetta 1). `checked()` rifiuta tutto il
     pacchetto se: le persone non sono esattamente i membri dei suoi W (prima una persona fuori dai membri teneva il
     suo nome, anche quello di un admin del centrale, con l'hash della macchina); un riferimento a una persona (ruolo
     personalizzato, server, token, identità SSO, grant) non è uno di quei membri; un nome non è un nome utente o di
     workspace; un ruolo non è `user`, `developer` o `admin`; un hash non è PBKDF2-SHA256 da 1.000 a 1.200.000
     iterazioni (o il segno di chi non ha ancora una password); un valore non è del suo tipo o della sua misura.
     `summary()` gira prima del salvataggio, e ogni errore del pacchetto è un 502 nel registro, mai un record salvato.
   - **Il proxy** decodifica il percorso (fino a tre volte) e rifiuta segmenti `.`, `..` e vuoti: prima
     `api/%2e/identity/…` passava il blocco perché il client lo normalizza (la macchina lo rifiutava comunque,
     `person`). Vale anche per `api/pages`.
   - **La prova del TOTP** è a livello di funzione: `Store.check(link, persona, password, codice)` (lo stesso hash, lo
     stesso segreto, il passo segnato: lo stesso codice non vale due volte). Il PBKDF2 gira fuori dal lock; un nome
     sconosciuto paga un hash finto (come `Authenticator`); un hash fuori misura o un passo che non è un intero danno
     `False`, mai un'eccezione. Il login delle persone importate sulle pagine del centrale è della fetta 2.
   - **Provato** (25/11/2026, lab3 centrale e lab4 macchina, codice del ramo `ad018-s1-import`): lab4 con 2 W e 6
     persone (2 app TOTP, 1 passkey su `omnia-lab4.example.net`, 1 SSO finto, 1 token di W, 1 grant, 1 ruolo
     personalizzato), `mario` anche su lab3. La prova a vuoto di lab3 contro i file di lab4 letti senza il codice di
     OmniaCLI: 14 voci confrontate, 0 differenze. Dopo l'import: 6 hash su 6 uguali, 2 segreti TOTP su 2, 1 chiave di
     passkey su 1; `mario` arriva come `mario.beta`, il `mario` di lab3 resta il suo. Un codice calcolato dal segreto
     importato passa `Store.check` su lab3, lo stesso codice una seconda volta no, una password sbagliata no. 41 valori
     segreti cercati (per impronta) in 1 MB di pagine e JSON visti dal browser, in 4 viste (1440 e 375, chiaro e
     scuro): 0 trovati; 0 errori di pagina, 0 dialoghi del browser, 1 pulsante primario. I file d'identità di lab4 sono
     identici byte per byte prima e dopo 3 prove a vuoto, 3 import e 2 cancellazioni.
   - **Scostamenti.** (1) Il pacchetto è JSON sul collegamento (che è SSH), non sigillato come quello della consegna:
     il collegamento è già cifrato e autenticato, e la macchina non ha una chiave del centrale. (2) Gli inviti in
     attesa si contano e non si importano (18.3.3 dice che si rifanno). (3) L'import vive in un file per collegamento,
     non ancora in W del centrale: i W remoti sono la fetta 2.
   - **Token e grant con un nome già preso** (la seconda revisione). Un token del pacchetto con il nome di un token
     del centrale o di un altro import arriva come `<nome>.<workspace>` (poi `-2`), con la sua persona in
     `token_people`: è lo stesso token, cambia solo il nome che registro e pagine dicono; la prova a vuoto lo elenca
     («Nomi di token già presi qui»). Un grant OAuth ha l'id della sua chiave (16 esadecimali) e il nome che gli dà
     `OAuthStore`, `oauth:<la sua persona>:<client>:<id[:4]>`, riscritto con la persona rinominata; un id o un nome
     (di prima o di dopo) che il centrale o un altro import ha già rifiuta il pacchetto: `OAuthStore` trova il grant di
     un token per nome. Un nome di token ripetuto in due W dello stesso pacchetto lo rifiuta.
   - **Un W con il nome di uno del centrale o di un altro import** (la seconda revisione): la fetta 1 lo segnala
     soltanto, come bloccante (`plan()["blocking"]`, la riga sulla pagina), e l'import si tiene. **La fetta 2 deve
     rifiutare o rinominare un tale W prima di leggere il record**: membri, ruoli, token, SSO e grant sono legati al W
     per nome, e leggerli sotto il W del centrale darebbe a persone e token della macchina l'ambito di un altro
     cliente.
   - **Resta aperto.** La regola per due W con lo stesso nome (rifiutare o rinominare) la decide la fetta 2, con il
     vincolo qui sopra. Il nome del portale del centrale come RP ID è la fetta 3: oggi il confronto usa il nome esatto
     del portale. `Store.check` riconfronta sotto il lock l'hash riletto con quello verificato: un import rifatto nel
     frattempo con un'altra password dà `False`.
2. **Il centrale casa** (18.2): W remoti, ambito su ogni route, REST/MCP inoltrati, liste, spostati diretti all'host.
   - *Prova*: la parità del vincolo 3, con 0 differenze. Il token di W su lab3 chiama `/api/v1` di un server su lab4. La
     voce `on_behalf` è nel registro di lab4.
   - *Si annulla*: con il rilascio di prima. Il record di casa su lab4 torna com'era.
   - *Stima*: 5 g.

   **Com'è nel codice (fetta 2).** `remotehome.py`, più il codice in `app.py` accanto all'import della fetta 1 e
   all'ambito delegato di AD-014.
   - **L'interruttore, per collegamento, spento di default.** Su Collegamenti › Identità «Servili qui…» (REST
     `POST /api/v1/links/{name}/identities/home`, MCP `identity_home`, con `confirm` = il nome del collegamento e
     `on`) scrive `workspace-homes.json` sul centrale (600, nel backup cifrato e nel purge) e lo dice alla macchina
     con `POST /api/identity/v1/home`: solo il collegamento admin del centrale, mai una persona, un token o un ambito.
     La macchina tiene `central-home.json` (600, nel purge): i W nativi la cui casa è quel collegamento. Non è
     `hosted-workspaces.json`: un W nativo non incontra mai il codice degli spostamenti (vincolo 8.2). Il login, le
     pagine e i membri della macchina restano (niente congelamento: fetta 5). Spento, il file sulla macchina si
     toglie e le sessioni sul centrale si chiudono.
   - **La regola dei W con lo stesso nome** (il MUST della fetta 1): **rifiutati, mai rinominati.** Un W dell'import
     con il nome di un W del centrale o di un altro import non si legge e non si dice alla macchina; le sue persone
     hanno gli altri W, e la pagina dice quali e perché. Con tutti presi, l'interruttore è rifiutato; un import che
     prenderebbe un W già servito per un'altra macchina è rifiutato (409). *Perché non rinominare*: la macchina
     conosce il W col suo nome (percorsi, job, registro, modelli delle pagine); un nome tradotto nei due versi su ogni
     risposta è una mappa che la parità a 0 differenze non può provare, e il cliente leggerebbe un nome diverso da
     quello dei suoi record. Rinominare, se serve, si fa sulla macchina, poi un nuovo import. A runtime la regola è
     *fail closed*: un W che diventa preso dopo (un W nuovo del centrale) non si serve più per nessuno dei due.
   - **Il login.** Sul login del centrale, dopo un utente del centrale, una persona servita: lo stesso hash
     (`Store.check_password`, un nome sconosciuto paga un hash finto) e, se l'aveva, lo stesso segreto TOTP
     (`Store.check_code`: un codice vale una volta). La politica di Carlo: il cliente tiene quello che aveva. Chi ha
     solo una passkey, o nessun fattore mentre il centrale ne chiede uno, è rifiutato col motivo e entra sulla
     macchina come prima (passkey e attivazione: fette 3 e 5). Le sessioni sono `remote-sessions.json`, separate da
     quelle del personale: un cookie dell'una non si legge mai come dell'altra. Un nome che è anche un utente del
     centrale non è mai servito (vince il centrale). L'account del cliente sul centrale si legge e non si cambia:
     password, fattori e sessioni si cambiano ancora sulla macchina (409 col motivo).
   - **Le richieste** (`RemoteRoutes`, poi `/.remote/<percorso>`): il primo controllo è del centrale, sulla stessa
     route con le stesse regole (`WORKSPACE_ROUTES`, i suoi W, i server dei suoi W come la macchina li elenca per
     lui, il ruolo personalizzato); poi la macchina con l'ambito (`X-OmniaCLI-Scope`, con `also`: gli altri W della
     stessa persona sulla stessa macchina), il tetto del ruolo è il suo nel W della richiesta, e la macchina
     ricontrolla (`delegated_scope` accetta `central-home.json`). Senza prefisso e senza l'indirizzo della macchina:
     i percorsi sono quelli del centrale. Le pagine (`CUSTOMER_ROUTES`) si disegnano dal modello di AD-017, che la
     macchina ora dà anche a una persona delegata, controllato sulla route della pagina; dalla macchina passano solo
     dati (`is_data`), `nosniff`, nessun `Set-Cookie`, nessun `Location` fuori dal centrale; i nomi del fornitore
     tolti (`placed_text`). Gli indirizzi che il cliente usa (MCP, REST, ritorno dello SSO) sono del centrale. Il
     ruolo che il modello dice non supera mai il più alto della persona.
   - **REST e MCP**: un token di W importato chiama `/api/v1` e `/mcp` del centrale; si chiede alla macchina con
     l'ambito `kind: token` e il nome del token sulla macchina; le regole di un token sono quelle della macchina.
   - **Liste e tempi**: le liste (`/api/servers`, `/api/instances`, `/api/backups`, `/api/jobs`, `/api/bell`…)
     entro `LIST_TIMEOUT` (5 s), le pagine e le altre letture entro 60 s, le azioni 600 s. Ogni persona viene da un
     solo import (i nomi sono unici fra gli import), quindi le sue righe sono di una macchina: l'unione è quella che
     fa la macchina (anche dei suoi server spostati, AD-014). Una macchina che non risponde è «la macchina del
     fornitore», mai il nome.
   - **Registro**: sul centrale ogni richiesta inoltrata (le scritture tutte, le letture una volta in `READ_GAP`)
     con persona o token, workspace, `session` (`session <sid>` o `token <nome>`) e `via` (il collegamento); sulla
     macchina il collegamento con `on_behalf` = la persona, nel workspace.
   - **Provato.** Nei test (`tests/test_remote_home.py`, 17): la parità del vincolo 3 su 176 route di
     `WORKSPACE_ROUTES` × admin, developer, user e un ruolo personalizzato, 644 richieste, 36 pagine con gli stessi
     controlli e lo stesso `<main>` (le pagine che la richiesta stessa cambia, registro e job, con gli stessi
     controlli), **0 differenze**; una persona di beta su ogni route coi nomi di acme e viceversa: 0 raggiunte, la
     macchina mai chiesta. Su lab3/lab4 (25/11/2026, codice di questo ramo): `acme` di lab4 rifiutato (lab3 ha il suo
     `acme`), `beta` servito con 4 persone (admin con app, developer, user, ruolo personalizzato senza i log) e 1
     token; 57 route GET × 4 persone, 228 richieste, 32 pagine uguali, **0 differenze**; la persona di beta sui nomi
     di acme: 0 raggiunte; il token di beta su lab3: `/api/v1/instances` → solo `beta-shop`, l'istanza di acme 403,
     MCP `list_servers` → `beta-shop`, un token sbagliato 401; nel registro di lab4 `link lab3, on_behalf t-dario,
     workspace beta, server beta-shop`; le due catene integre (`verify-access-log`).
   - **Scostamenti.** (1) I W con lo stesso nome si rifiutano uno per uno invece di rifiutare il collegamento intero.
     (2) Il record di casa sulla macchina è `central-home.json`, non `hosted-workspaces.json` (vincolo 8.2). (3) Le
     modifiche d'identità del cliente (password, fattori, sessioni, revoca OAuth) restano sulla macchina fino alla
     fetta 5; quelle del W fatte dalla pagina del W (membri, token, ruoli, SSO) vanno alla macchina, e arrivano sul
     centrale col prossimo import. (4) Il login delle persone servite lo controlla la macchina: col centrale acceso e la macchina ferma non entrano (prima,
     con l'hash importato, sì; ma allora la macchina non serve comunque le loro pagine).
   - **La revisione (dopo `133fa50`).** Il centrale non è la fonte delle identità finché la macchina non passa
     (fetta 5): la macchina lo è, e decide.
     - **Sulla macchina** `delegated_scope` (anche per MCP) ricontrolla l'ambito di un W in `central-home.json`
       contro i suoi record di adesso, `also` compreso: la persona è ancora membro (ruolo `workspace`), il ruolo è al
       più il suo lì, il ruolo personalizzato è il suo `ws.grant`; il token esiste ancora ed è di quel W, il livello è
       al più `token_role` (mai più del ruolo della sua persona), CI e server sono i suoi (`token_servers`: la REST lo
       controlla per nome, le letture delle pagine lo rifiutano come `identify`, MCP restringe il grant). Altrimenti
       l'ambito è rifiutato (403, nel registro).
     - **Sul centrale** il livello di un token servito è al più il ruolo della sua persona (come `config.token_role`).
       Il login lo controlla la macchina (`POST /api/identity/v1/login`, solo il collegamento admin del centrale):
       password, poi il codice con lo step che la password ha aperto lì (anche un codice di recupero), un solo
       contatore dei codici, quello della macchina. Se la risposta (impronta dell'hash, mai l'hash; W e ruoli) non è
       l'import, il centrale rilegge l'import prima della sessione. Dopo una scrittura inoltrata riuscita sulle
       persone di un W (membri, token, ruoli, SSO) il centrale rilegge l'import prima di rispondere; un import più
       vecchio di 10 minuti si rilegge da solo in background. Un `Location` della macchina passa solo se, letto come un
       browser (controlli e spazi tolti, `\` come `/`), è un percorso del centrale: niente schema, niente host.
     - Prova: `tests/test_remote_home_review.py`, 15 test, 10 falliscono su `133fa50` (token cancellato dal centrale e
       sulla macchina, membro tolto dal centrale e sulla macchina, ruolo abbassato e password cambiata sulla macchina,
       codice usato sulla macchina e codice di recupero, token oltre il ruolo della persona, redirect con `\`,
       import vecchio riletto).
   - **La terza lettura (dopo `d4e5e49`).** (1) Con la macchina ferma o lenta il login di un nome servito risponde come
     una password sbagliata (401, l'hash finto pagato), la ragione resta nel registro: prima della password niente
     distingue un nome servito da uno sconosciuto. (2) `/api/identity/v1/login` usa il `LoginLimiter` della macchina:
     gli errori dal centrale e dal modulo della macchina contano nella stessa finestra per nome (per indirizzo a parte,
     `central:<link>:<persona>`), e una persona in attesa lì è rifiutata anche dal centrale. (3) Un W che la persona
     (o il token) non ha più sulla macchina si toglie dall'ambito, gli altri restano suoi; senza nessun W rimasto il
     rifiuto lo dice («Non sei più membro di questo workspace»). Prova: 3 test in più in
     `tests/test_remote_home_review.py`, che falliscono su `d4e5e49`.
   - **Resta aperto.** (a) **Il server spostato direttamente all'host** (punto 7 di 18.2): l'host prende l'ambito
     solo dalla casa scritta in `hosted-workspaces.json`, che è la macchina di partenza; cambiarla è 18.7 (fetta 6).
     Oggi i dati passano per la macchina di partenza (due salti) e le pagine di un server spostato dicono «non
     disponibile». (b) I grant OAuth importati (legati all'URL MCP della macchina) non si inoltrano. (c) Il login con
     SSO e con passkey delle persone servite (fetta 3). (d) I websocket: nessuna route di `WORKSPACE_ROUTES` ne usa.
3. **Nomi e bordo**: due nomi, `portal_admits` nei due versi, RP ID come impostazione, pagina di manutenzione nginx.
   - *Prova*: una passkey (autenticatore virtuale di Playwright) fatta sul nome A vale su A e su un altro nome dello
     stesso RP ID, ed è rifiutata su un nome di un altro dominio. Il personale è rifiutato sul portale e i clienti sul
     pannello, con 403 nel registro. Con OmniaCLI fermato su lab3, nginx risponde con la pagina statica.
   - *Stima*: 2 g.

   **Com'è nel codice (fetta 3).** `config.py` (`Options.portal_panel`, `Options.passkey_domain`), `passkeys.py`
   (`rp_here`, `origins_here`, `verify_given`), `workspaceweb.py` (il sito con due nomi e la pagina di manutenzione),
   `workspacehelper.portal`, e in `app.py` `portal_admits`, `portal_act`, il login delle persone servite con passkey.
   - **Due nomi, un sito.** *Opzioni › Indirizzo del portale* ha in più **Pannello del personale**. **Pubblica**
     chiama `workspace-helper portal <portale>,<pannello>` (un argomento, la virgola: le righe di sudoers non
     cambiano): un solo `omniacli-_portal.conf` con `server_name` dei due nomi, un certificato di Let's Encrypt per
     tutti e due se tutti e due portano a questo nginx, altrimenti uno della macchina con i due nomi nel SAN. Le
     Opzioni salvano `portal.panel`; il Salva delle Opzioni non lo cambia (cambia solo con la pubblicazione); una
     pubblicazione che non dice `panel` tiene quello di adesso. I nomi delle istanze Odoo non cambiano.
   - **`portal_admits` nei due versi.** Sul nome del portale il personale è rifiutato dopo la password giusta, come
     prima; sul nome del pannello i clienti: gli utenti dei workspace del centrale e le persone servite per una
     macchina collegata (fetta 2). 403 «Questo indirizzo è il pannello del personale del fornitore…», nel registro
     un `failed` con `status: 403` («a user of a workspace at the staff's panel»); una password sbagliata resta un
     401. Vale per il modulo, la passkey (dopo la verifica), la sessione (`identify`: il cookie di un cliente sul
     pannello è 403) e per le persone servite (dopo che la macchina ha detto sì).
   - **RP ID come impostazione** (`portal.passkeys`, *Dominio delle passkey*, REST `POST /api/v1/portal/passkeys`, MCP
     `portal_passkeys`). `passkey_rp` dà il dominio quando il nome della richiesta è uno dei due nomi del centrale e
     sta sotto il dominio; su ogni altro nome il nome stesso, come prima. Le origini attese di una cerimonia sono
     solo quelle dei due nomi sotto il dominio (`Ceremony.origins`): un sottodominio estraneo (un Odoo dello stesso
     dominio) ottiene dal browser un'asserzione dell'RP ID, e il centrale la rifiuta per l'origine. Il dominio vuole
     almeno due etichette e i due nomi sotto; cambiarlo quando passkey fatte finora smetterebbero di funzionare
     chiede `confirm` = il dominio (o `off`) e dice quante. La prova a vuoto della fetta 1 confronta le passkey con
     questo RP ID (`plan(rp_id=…)`).
   - **Le passkey delle persone servite** (lasciate aperte dalla fetta 2). Il centrale fa la cerimonia, la macchina
     verifica. `POST /api/identity/v1/login` con `rp`, dopo la password, risponde `need: passkey` con uno `step` e gli
     id delle passkey di quell'RP ID; poi `passkey` (credenziale, challenge, RP ID e origini del centrale) con lo
     `step` (secondo passaggio) o senza (accesso senza password, verifica dell'utente obbligatoria). La macchina
     controlla con la sua chiave e il suo contatore (uno solo, il suo), l'handle e l'appartenenza, e registra
     «<persona> through the central panel: passkey «…»». Il centrale trova la persona di un accesso senza password
     dall'id della credenziale importato (`Person.passkeys`: solo id e RP ID). Una passkey fatta su un altro nome
     non è di questo RP ID: la persona lo legge dopo la password («una passkey fatta su un altro indirizzo»).
   - **`public_base`** resta il portale anche sul nome del pannello: invito di un cliente, URL MCP e risorsa OAuth,
     ritorno dello SSO, Aiuto. L'invito di una persona del personale porta al pannello.
   - **La pagina di manutenzione** è nel sito stesso: `error_page 502 503 504 =503 @omniacli_maintenance` e
     `return 503 '<html…>'` (nessun file da tenere né da cancellare nel purge), in italiano e in inglese, con HSTS,
     `Cache-Control: no-store` e `Retry-After: 30`. Solo gli errori di nginx: le risposte di OmniaCLI passano come
     sono (niente `proxy_intercept_errors`).
   - **REST e MCP**: `GET /api/v1/portal`, `POST /api/v1/portal/publish` (un job, 202), `POST /api/v1/portal/passkeys`;
     `portal_status`, `portal_publish`, `portal_passkeys`. Solo gli admin della macchina, mai un token di CI, di un
     workspace o un collegamento; ogni scrittura nel registro.
   - **Provato.** Nei test (`tests/test_portal_names.py`, 14 test: 13 falliscono su `6ea4649`, il quattordicesimo è
     `nginx -t`, saltato dove nginx non c'è). Su lab3 (centrale) e lab4 (macchina), il 25/11/2026, con il codice di
     questo ramo, i nomi `portale.172-30-0-13.sslip.io` e `pannello.172-30-0-13.sslip.io`, l'RP ID
     `172-30-0-13.sslip.io` e Chromium con l'autenticatore virtuale:
     - una passkey fatta sul pannello (RP ID del dominio) entra sul pannello e sul portale (200 su `/api/servers`).
       Su `pannello.172-30-0-13.nip.io` (altro dominio, ripubblicato per la prova) l'RP ID è il nome stesso e il
       browser non la offre (`NotAllowedError`; l'RP ID del dominio da lì: `SecurityError`). Da
       `odoo.172-30-0-13.sslip.io` il browser dà l'asserzione e il centrale risponde 401;
     - l'admin sul portale: 403 dopo la password. `cliente-s3` (acme di lab3) sul pannello: 403, con una password
       sbagliata 401. Nel registro di lab3, `failed` con `status: 403` per tutti e due;
     - una persona di beta di lab4 fa la passkey sul portale di lab4 (`macchina.172-30-0-13.sslip.io`, stesso
       dominio). lab3 importa e serve beta (acme rifiutato: lab3 ne ha uno). La persona entra su lab3 senza
       password e come secondo passaggio, e vede solo `beta-shop`; sul pannello ha 403, con la password e con la
       passkey. Nel registro di lab4: «cliente-beta through the central panel: passkey «Telefono di beta», without
       password» e «… password + passkey …»;
     - OmniaCLI fermato su lab3: i due nomi rispondono 503 con la pagina statica (anche un POST all'API), a 1440 e
       375, chiaro e scuro. Riacceso, `/login` risponde 200;
     - le Opzioni in italiano, a 1440 e 375, chiaro e scuro: 0 errori di pagina, 0 dialoghi del browser, 1 pulsante
       primario, nessuno scorrimento orizzontale. lab3 e lab4 sono tornati alla 2026.1.5525 con i loro file di prima.
   - **Scostamenti.** (1) La pagina di manutenzione sta nel sito del portale di ogni macchina che lo pubblica, non
     solo del VPS: è la stessa riga di nginx, e anche una macchina con il portale la usa. (2) Tolto dalla revisione
     (sotto): il secondo fattore sul pannello è per persona, obbligatorio di default. (3) I connettori OAuth
     del personale: la risorsa MCP è quella del portale anche sul pannello, dove il personale non dà il consenso, e
     il personale usa i token Bearer. (4) Una passkey di un cliente usata sul pannello viene rifiutata dopo la
     verifica della macchina, che registra il login prima del rifiuto del centrale: le due voci si leggono insieme.
   - **La revisione (dopo `423da12`).**
     - **Il secondo fattore sul pannello, per persona** (la risposta 2 di Carlo; toglie lo scostamento 2). Sul nome
       del pannello un utente della macchina dà il secondo passaggio, qualunque sia la politica, salvo che un altro
       admin lo abbia reso facoltativo per lui (`portal.panel_optional`; *Utenti* › «Pannello senza secondo
       passaggio…», REST `POST /api/v1/portal/panel-second-factor`, MCP `portal_panel_second_factor`, nel registro
       come `second-factor`). **Il default è obbligatorio**: il pannello è su internet e la sessione di una persona
       del personale raggiunge tutte le macchine dei clienti, quindi una password rubata da sola non deve bastare
       finché qualcuno non sceglie altrimenti, con il nome e la data nel registro. Nessuno se lo toglie da solo (è
       la prima cosa che farebbe chi ha rubato la password). La pagina Utenti lo dice accanto a chi ce l'ha
       facoltativo, e in più se non ha un secondo fattore. Sull'indirizzo della macchina (collegamento, tunnel) non
       cambia nulla.
     - **Il certificato di Let's Encrypt non si cancella mai prima del nuovo.** Prima, pubblicare portale e
       pannello con il DNS del pannello non ancora fatto cancellava il certificato buono del portale e serviva uno
       della macchina dietro HSTS di un anno: i clienti restavano fuori. Ora certbot gira con `--cert-name` e i
       nomi di adesso solo quando tutti portano qui, e sostituisce lo stesso lineage solo se lo rilascia; se no il
       certificato di prima continua a servire e il job dice il nome che manca (`kept.without`).
     - **Un nome che il sito non ha.** Il sito del portale è il primo di `conf.d`, quindi il server di default di
       nginx: un `Host` qualsiasi (un indirizzo IP, un nome del client) arrivava a OmniaCLI e saltava sia
       `portal_admits` sia il secondo passaggio. Ora una `map $host` con i due nomi: 421 sulla 443 e 444 sulla 80
       per ogni altro nome; i siti delle istanze hanno i loro `server_name` e non passano di lì. E OmniaCLI, se
       arrivasse comunque in https su un altro nome con il portale pubblicato, lo tratta come il portale (il
       personale rifiutato, il secondo passaggio del portale): chiuso, non «tutti come prima».
     - **Un token di alcuni server** non tocca il portale anche via REST (prima solo MCP lo rifiutava).
     - **Un suffisso pubblico** (`sslip.io`, `nip.io`, `co.uk`, `github.io`…) non è un dominio delle passkey:
       una lista incorporata dei più comuni (nessuna dipendenza nuova: la Public Suffix List intera non c'è fra
       quelle di oggi).
     - Prova: 5 test nuovi e uno cambiato in `tests/test_portal_names.py`, che falliscono su `423da12`.
   - **Resta aperto.**
     - le passkey fatte sul nome di una macchina (18.4.5: il nome vecchio servito per 90 giorni, oppure rifatte con
       il link monouso della fetta 5);
     - una passkey nuova fatta sul centrale da una persona servita (il suo account sul centrale si legge e non si
       cambia: fetta 5);
     - i connettori OAuth del personale sul pannello;
     - il login SSO delle persone servite.
4. **Webhook della CI sul nome dell'istanza** (18.5.1).
   - *Prova*: un webhook sull'indirizzo del portale viene riscritto sul nome dell'istanza, con lo stesso id. Con lab3
     fermo, un push fa il deploy della test su lab4.
   - *Stima*: 1 g.

   **Com'è nel codice (fetta 4).** In `app.py`, accanto al codice dei webhook di GitHub e all'interruttore della fetta 2.
   - **Quando.** L'interruttore «Servili qui…» del centrale manda alla macchina anche l'indirizzo del suo portale
     (`POST /api/identity/v1/home`, campo `portal`, solo `https://<nome>`); la macchina lo tiene in
     `central-home.json`. Se cambia il centrale di un W (acceso, spento, un altro portale), la macchina avvia un job
     normale, «GitHub webhooks of <W>», con le sue righe e il suo esito nel registro della macchina. Non parte se i
     server di quei W non hanno né una CI accesa né un webhook fatto da OmniaCLI. La risposta dell'interruttore (pagina,
     REST, MCP `identity_home`) porta l'id del job, `hooks_job`.
   - **Cosa riscrive.** Il job fa la stessa sincronizzazione di «CI accesa di nuovo» (`github_sync`): stesso `rid`,
     stesso segreto, il webhook trovato per id e cambiato con `PATCH`, mai rifatto. Con il token GitHub che le istanze
     hanno già. La riga del job dice «moved from … to …, the same webhook with the same secret».
   - **L'indirizzo** (`github_hook_base`). Per un W servito da un centrale l'ordine cambia:
     1. il nome di un'istanza;
     2. il portale del centrale;
     3. il portale della macchina.

     Per gli altri W resta quello di oggi: il portale della macchina, poi il nome. Spento l'interruttore, i webhook
     tornano così.
   - **L'istanza senza nome pubblico** va sul portale del centrale. Il centrale riceve `/hooks/github/<id>`, che non è
     suo, e lo passa alla macchina com'è arrivato, attraverso il collegamento:
     - passano il corpo e le intestazioni di GitHub (firma, evento, consegna, content type), mai i cookie;
     - passa la risposta della macchina;
     - il centrale non ha il segreto: la firma la controlla la macchina;
     - una macchina che non risponde dà 502;
     - un id di nessuno dà 403, e alle macchine non arriva niente.

     Gli id da inoltrare li dice la macchina, nella GET `/api/identity/v1/home` (`hooks`), solo al collegamento admin
     del centrale e senza segreti. Il centrale la richiede al più ogni 30 s, quando GitHub chiama un id che non conosce.
     Ogni inoltro sta nel registro del centrale, con `via`.
   - **La pagina.** La riga del webhook nella scheda Moduli dell'istanza è un avviso: «L'istanza non ha un nome
     pubblico: GitHub chiama il pannello centrale… questa CI dipende dal pannello centrale». Lo dice anche il job. La
     pagina Identità del centrale dice che i webhook seguono l'interruttore.
   - **Provato.** Nei test, `tests/test_central_hooks.py`, 3 test con un GitHub finto: tutti falliscono su `6ea4649`.
     Sul lab il 25/11/2026, con il codice di questo ramo sul pacchetto 2026.1.5525. Il collegamento è HTTP su
     127.0.0.1, non SSH. lab3 e lab4 erano occupati da un'altra fetta, e un secondo container non partiva: l'host ha
     finito le istanze inotify (128). Per questo su un `lab5` usa e getta, poi eliminato, girano due processi:
     - la macchina, il servizio `omniacli` sulla 8090;
     - il centrale, un secondo OmniaCLI dallo stesso venv, cartella e porta sue (8091), unità transitoria
       `omniacli-central`.

     Il GitHub finto è `fakegh.py` (API e git su https, una CA del lab nel trust store). nginx serve i nomi dei webhook
     con un certificato autofirmato. Il W `acme` ha `acme-main`, di tipo generico: test sul branch `test`, riavvio da
     uno script init.d. I nomi puntano a 127.0.0.1. I risultati:
     - CI accesa: webhook 1 su `https://portal.omnia-lab5.example.net/hooks/github/Ig50LFYe7hDp`;
     - interruttore acceso: job di 1 s, il webhook 1 va su
       `https://main.test.acme.omnia-lab5.example.net/.omniacli/hooks/github/Ig50LFYe7hDp`, con la stessa impronta del
       segreto e senza nessun POST di un webhook nuovo;
     - centrale fermato (8091 non risponde): un commit sul branch `test`, e la consegna del GitHub finto al nome dà
       202, deploy `success`; la test è sul commit spinto (`4de9348`);
     - interruttore spento: il webhook 1 torna sul portale della macchina;
     - W senza dominio, interruttore acceso: il webhook 1 va su `https://central.omnia-lab5.example.net/hooks/…`; la
       consegna passa dal centrale (202, «forwarded to lab5m: 202» nel suo registro) e fa il deploy di `a142fbe`;
     - centrale fermato: la consegna dà 502 (nginx), niente deploy. È la dipendenza che la pagina dichiara;
     - spento di nuovo: torna sul portale della macchina.

     I segreti (webhook, token finto, token del collegamento, password) cercati in 32 file delle due cartelle, tranne
     i file che li tengono: 0 trovati. La consegna l'ha fatta il GitHub finto, non GitHub: nessun repository vero è
     stato usato.
   - **Scostamenti.** (1) Il «passaggio» della fetta 5 non c'è ancora: oggi a riscrivere è l'interruttore della fetta 2,
     e la fetta 5 lo riusa. (2) Il portale della macchina resta come ultima scelta, finché la fetta 5 non lo toglie.
   - **Resta aperto.**
     - Un nome dato dopo all'istanza non riscrive il webhook da solo: serve riaccendere la CI, come dice la pagina.
     - Un portale del centrale che cambia indirizzo si dice alla macchina solo rifacendo l'interruttore.
     - Un GitHub che rifiuta durante il job lascia il webhook dov'era, con il motivo, e lo riprova la notte
       (`github_retry`).
   - **La revisione (dopo `5f417b1`).** Cinque correzioni:
     1. Il portale del centrale è di tutte le macchine servite. Lì il webhook di una macchina è solo quello al suo
        indirizzo esatto (base + il suo `rid`), mai un altro che comincia uguale. Prima una macchina prendeva il webhook
        di un'altra sullo stesso repository (`PATCH` con il suo `rid` e il suo segreto), e con la CI spenta lo toglieva.
     2. Il centrale non chiede più alle macchine al più ogni 30 s per tutti. Un id che non conosce le richiede: le
        chiamate in attesa dividono una sola richiesta, al più una al secondo. Un id che nessuna elenca è rifiutato
        subito per 30 s, quell'id soltanto. Prima un id a caso chiudeva la porta per 30 s anche al primo push di un
        webhook appena fatto, e GitHub non riconsegna.
     3. La macchina dice `{rid, id}`. Un `rid` elencato da due macchine non va a nessuna e il registro lo dice. Una
        chiamata con un `X-GitHub-Hook-ID` diverso dall'`id` della macchina è rifiutata.
     4. La riga del job dice il verso di ogni W: un interruttore può prenderne alcuni e restituirne altri.
     5. Il corpo di una chiamata di webhook (questo e quello per istanza) oltre `deployhooks.MAX_BODY` (5 MiB) è
        rifiutato con 413 prima di leggerlo, e non si inoltra.

     Prova: 5 test in più in `tests/test_central_hooks.py`, che falliscono su `5f417b1`.
5. **Il passaggio di una macchina e il ritorno** (18.3): congelamento, record di casa, login locale spento, inviti per il
   secondo fattore perso, 7 giorni, cancellazione dei segreti, registro unito.
   - *Prova* su lab4:
     - le persone entrano da lab3 con la stessa password e lo stesso TOTP;
     - su lab4 il loro login è rifiutato;
     - chi aveva solo una passkey sul nome di lab4 non può attivare un fattore con la sola password, e lo fa dal link;
     - «Torna sulla macchina» porta indietro una password cambiata su lab3;
     - dopo i 7 giorni (orologio finto) su lab4 ci sono 0 segreti di persone.
   - *Stima*: 3,5 g.

   **Com'è nel codice (fetta 5).** `identitypass.py`, più il codice in `app.py` accanto all'interruttore della fetta 2
   (sulla macchina e sul centrale), `identityimport.Store` (`passed`, `set_passed`, `mutate`), `PasskeyStore.adopt`.
   - **Sulla macchina** una POST interna, `/api/identity/v1/pass` con `op`, solo il collegamento admin del centrale che
     è la casa di quei W (come `/home`), ogni passo nel registro:
     - `freeze`: lo stato `frozen` nel record di casa (`central-home.json`, sotto `pass`). Da lì le modifiche
       d'identità di quei W sono rifiutate con 409 «Questo workspace passa a <portale>», nel registro: le route
       `{workspace}` di membri, inviti, reset del secondo fattore, token, ruoli, SSO (anche quando arrivano dal centrale
       con l'ambito), l'Account delle loro persone (password, app, passkey, revoca OAuth), e da Utenti le persone e i
       token di quei W. Le azioni sulle istanze no. Una persona che è anche in un W della macchina che non passa
       rifiuta il passaggio: passano insieme o per niente.
     - `pass`: lo stato `passed`, con lo `snapshot` (membri, ruoli, ruoli personalizzati, server, token con livello,
       CI e server, `revision`; mai un hash o un segreto) e `until` = 7 giorni (`placement.COPY_DAYS`). Da lì
       `delegated_scope` controlla l'ambito del centrale contro lo snapshot (`passed_records`, le stesse regole di
       `own_records`), il login di quelle persone è spento (modulo dopo la password giusta, «Il tuo accesso è passato a
       <portale>», passkey, SSO, inviti, sessioni chiuse e non più lette, token su REST e MCP 401,
       `/api/identity/v1/login` del centrale `{ok: false, passed: true}`), e l'export e la prova a vuoto non le
       contengono più (la copia è illeggibile, anche per il centrale). Il portale della macchina non è più l'ultima
       strada dei webhook della loro CI.
     - `return`: le persone mandate dal centrale (`identitypass.returned`: solo nomi dello snapshot, hash, app e
       passkey con i controlli dell'import; altrimenti 400 e niente cambia), prese (`users.yaml`, `second_factors.adopt`,
       `PasskeyStore.adopt`); il record di casa si toglie, il login locale si riaccende, il job dei webhook li riporta.
       Dopo la cancellazione: 409, «tornano con una consegna».
     - **I 7 giorni**: `pass_loop` guarda ogni ora (e all'avvio); un job «Segreti delle persone di <W> cancellati dopo
       il passaggio» toglie hash (l'utente, se non è di un altro W che resta), app, passkey, sessioni, SSO con il client
       secret, token del W, grant, inviti, e i loro nomi dai membri; la riga del job e il registro dicono quanti.
   - **Sul centrale** `identity_act` ha `pass`, `return`, `factor_link` (pagina Identità, REST
     `POST /api/v1/links/{name}/identities/pass|return|factor-link`, MCP `identity_pass`, `identity_return`,
     `identity_factor_link`; solo admin del centrale, `confirm` = il nome del collegamento):
     - **Passa**: congela, importa di nuovo (dopo il congelamento), controlla che i W serviti siano gli stessi, passa;
       un errore a metà scongela (`return` senza persone). L'import porta `passed` e da lì è la fonte: un nuovo import
       e lo spegnimento dell'interruttore sono rifiutati (409), la rilettura della fetta 2 non parte più.
     - **Il login** delle persone passate lo controlla il centrale (`passed_login` risponde come la macchina, così
       `remote_login`, `remote_step` e `remote_passkey` non cambiano): l'hash, il codice dell'app con il passo
       segnato (o un codice di recupero), la passkey del suo RP ID con chiave e contatore importati.
     - **L'Account** di una persona passata è `account.html` con i suoi dati: password, app, passkey e sessioni si
       cambiano sul centrale (`passed_account`), con le stesse regole dell'Account; ogni cambio è segnato `changed` e
       alza la `revision` del passaggio.
     - **Vincolo 8.5**: chi ha solo una passkey di un altro RP ID, o nessun secondo fattore dove il portale ne chiede
       uno, al passaggio riceve un link monouso (`/second-factor/<token>`, 72 ore, una volta; il token mostrato una
       volta all'admin, conservato come hash in `second-factor-links.json`, 600, nel backup cifrato e nel purge). Con il
       link **e** la password imposta l'app e vede i codici di recupero; con la sola password il login dice di usare
       il link, e non apre mai l'attivazione.
     - **Torna sulla macchina** manda solo le persone di quel collegamento segnate `changed`, con i loro nomi sulla
       macchina; poi interruttore spento, sessioni chiuse, link finiti.
     - **Il registro unito**: la pagina del registro di un W servito unisce le voci della macchina e quelle del
       centrale con quel W, in ordine di tempo, senza i nomi delle macchine (`placed_text`).
   - **Provato.** Nei test (`tests/test_identity_pass.py`, 9 test, tutti falliscono sul commit di partenza `61c5426`).
     Su lab3 (centrale) e lab4 (macchina), il 25/11/2026, codice di questo ramo sul pacchetto 2026.1.5525: lab4 con
     `beta` di 3 persone (`b-anna` admin con app, `b-bruno` sviluppatore con solo una passkey su
     `omnia-lab4.example.net`, scritta nel file per la prova, `b-elena` utente senza secondo fattore); `acme` rifiutato
     (lab3 ne ha uno). Import, «Servili qui», «Passale»: 2 link monouso (b-bruno, b-elena: il portale di lab3 chiede il
     secondo passaggio).
     - `b-anna` entra su lab3 con la stessa password e lo stesso TOTP (303, 303, vede `beta-shop`); su lab4 403 «Il tuo
       accesso è passato a omnia-lab3.example.net: entra da lì.»; le voci «through the central panel» nel registro di
       lab4 restano 5 (nessun login chiesto alla macchina);
     - `b-bruno` con la sola password: 403 «Il secondo passaggio va impostato di nuovo su questo portale…», nessuna
       sessione, `/login/setup` non si apre; dal link: password sbagliata 401, password e codice 200 con i codici di
       recupero, di nuovo 410; poi password + codice 303. Lo stesso per `b-elena`;
     - `b-anna` cambia la password su lab3 (200); su lab4 l'hash vecchio resta (vecchia sì, nuova no); «Torna sulla
       macchina»: 3 persone rimandate; su lab4 la password nuova + lo stesso TOTP 303, la vecchia 401; `b-bruno` entra
       su lab4 con l'app impostata su lab3; su lab3 non è più servita (401);
     - di nuovo servito e passato; orologio finto (`until` del passaggio su lab4 portato a ieri, servizio riavviato):
       job `success`, «beta: … 3 hash di password, 3 app, 1 passkey, 0 token, 0 grant, 0 inviti»; dei 37 valori
       segreti di quelle persone presi prima (hash, segreti TOTP, codici di recupero, chiavi di passkey) **0** restano
       in qualunque file di `/var/lib/omniacli` e `/etc/omniacli`; 0 utenti, 0 membri, 0 app, 0 passkey. `b-anna` entra
       ancora su lab3 e vede `beta-shop`; «Torna» dopo i 7 giorni: 409;
     - il registro di `beta` visto da `b-anna` su lab3: 500 righe dei due lati, 0 volte «lab4»;
     - `verify-access-log`: catene integre su lab3 (4.612 voci) e lab4 (3.760);
     - Playwright in italiano, 1440 e 375, chiaro e scuro, 20 viste (Identità del personale con la conferma nella
       pagina, Account e registro del cliente, la pagina del link, il login su lab4): 0 errori di pagina, 0 dialoghi
       del browser, 1 pulsante primario dove c'è un'azione, nessuno scorrimento orizzontale. Fra 8,8 MB di pagine e
       JSON visti, password, nuova password e segreti delle app: 0; il token del link compare solo nella sua stessa
       pagina (i link della lingua, come `/invite`).
   - **Scostamenti.** (1) Lo stato del passaggio sulla macchina sta nel record di casa della fetta 2
     (`central-home.json`), non in `hosted-workspaces.json` (vincolo 8.2). (2) Il link monouso imposta l'app, non una
     passkey: la passkey si aggiunge poi dall'Account del centrale. (3) Il link chiede anche la password (link e
     password, mai la sola password). (4) Lo `snapshot` ha una `revision` ma il centrale non ne manda di nuove: membri,
     token, ruoli e SSO di un W passato restano congelati finché non si gestiscono dal centrale (sotto).
     (5) «Torna» manda chi ha cambiato qualcosa sul centrale, compresi un codice usato o un contatore di passkey.
     (6) La copia ferma «illeggibile dalle pagine» è letta come: export e prova a vuoto la escludono, le pagine non hanno
     mai mostrato segreti.
   - **Resta aperto.**
     - Gestire dal centrale membri, inviti, token, ruoli personalizzati e SSO di un W passato (con `sync` dello
       snapshot e la sua `revision`): oggi sono congelati (409 dalla macchina).
     - Il login SSO e i connettori OAuth delle persone passate (le grant importate non si servono).
     - Il nome del portale di una macchina servito dal centrale per 90 giorni (18.4.5): oggi le passkey di quel nome
       si rifanno con il link.
     - Il webhook di un'istanza senza nome pubblico di un W passato dipende dal centrale (come nella fetta 4).
   - **La revisione (dopo `fb3867d`).** Sei correzioni:
     1. Passaggio, ritorno e link di un collegamento uno alla volta sul centrale (un lock per collegamento), e sulla
        macchina ogni passo e la cancellazione dei 7 giorni sotto un solo lock. La macchina congela solo un W senza
        passaggio in corso. Prima un secondo «Passale» contemporaneo riceveva 409 e il suo annullamento (un `return`
        senza persone) disfaceva il passaggio riuscito: il centrale si credeva passato, la macchina riaccendeva il
        login e toglieva il record di casa.
     2. Un passaggio fallito a metà si annulla con `op: thaw`, che toglie solo lo stato `pass` e solo se `frozen`:
        il record di casa della fetta 2 resta. Prima un export fallito toglieva tutto il record, e il centrale
        mostrava «servito» mentre la macchina rifiutava l'ambito.
     3. Il `return` vale solo per un W `passed`, solo per il passaggio che il centrale ha registrato (il momento
        della macchina, `machine_passed`, va e torna) e solo entro i 7 giorni, sui due lati (prima la macchina
        guardava solo `purged`). La cancellazione rilegge lo stato sotto il lock: un W tornato non si cancella, e il
        segno `purged` si scrive solo su un record di casa che ha ancora il suo collegamento.
     4. Un link monouso si fa e si usa solo per chi ne ha bisogno adesso (`needs_link`, riletto anche sotto il lock
        dell'import): prima un admin poteva sostituire con link e password l'app di una persona.
     5. Il registro unito prende del centrale solo le voci chieste attraverso questo collegamento (`via`) da quando
        il centrale serve la macchina, e toglie i nomi di tutti i collegamenti: prima un W con lo stesso nome di un
        altro cliente mostrava le sue voci (persone, indirizzi).
     6. Lo SSO di un W congelato o passato non crea più utenti né membri: rifiutato prima di `sso_person`.

     Prova: `tests/test_identity_pass_review.py`, 9 test, tutti falliscono su `fb3867d`.
   - **La seconda revisione (dopo `a60ccb2`): una risposta persa.** Ogni passaggio ha un id del centrale, che ogni
     passo porta (`freeze`, `pass`, `thaw`) e che la macchina tiene nel suo stato (detto dalla GET `/home`, senza
     segreti). Un passo chiesto di nuovo con lo stesso id è lo stesso passo (la stessa risposta); `thaw` annulla solo
     il congelamento del suo id; un passaggio del collegamento prende il congelamento di un suo passaggio non finito
     (il centrale ne fa uno alla volta). Sul centrale il passaggio in corso è scritto nell'import (`passing`) finché
     non finisce. Se la risposta di `pass` si perde, il centrale rilegge lo stato: passato con il suo id, il
     passaggio si chiude anche qui; altrimenti si annulla. Se si perde quella di `freeze`, il congelamento si annulla.
     Se non si legge nemmeno lo stato, la pagina Identità lo dice al prossimo caricamento («La macchina ha passato…» o
     «… tiene congelati…») e offre **Riallinea con la macchina…** (REST `…/identities/reconcile`, MCP
     `identity_reconcile`, conferma nella pagina, nel registro): un passaggio con l'id di questo pannello si chiude
     qui, un congelamento senza passaggio in corso si annulla; un passaggio che questo pannello non ha fatto non si
     adotta mai (detto, 409). Prova: `tests/test_identity_pass_lost.py`, 5 test, tutti falliscono su `a60ccb2`.
6. **Consegna e spostamento con l'identità centrale** (18.7).
   - *Prova*: con il NAT simulato di AD-008 §9 passo 7, una consegna da lab4 il cui codice punta a un nome del
     workspace. Il congelamento a `ready`. Le persone importate con hash e TOTP dalla macchina che riceve. Al commit
     l'ambito di W è rifiutato subito. Uno spostamento lab4 → un terzo lab, servito da lab3 senza passare da lab4.
   - *Stima*: 3 g.
7. **Il VPS** (la fetta chiesta da Carlo in AD-017):
   - provisioning, firewall, la release;
   - il ripristino dal backup cifrato (AD-017 fetta 7);
   - i collegamenti SSH che partono dal nuovo indirizzo.
   - *Prova*: il centrale ripristinato sul VPS apre lab4 dal suo tunnel. Il tempo del ripristino è misurato.
   - *Si annulla*: il PC di Carlo resta il centrale finché il VPS non passa la prova.
   - *Stima*: 2 g.

Totale ≈ 20 giornate. Poi i clienti, una macchina alla volta, ciascuna con il sì di Carlo: la fetta 5 sulla macchina vera,
con la misura della fetta 0 di quella mattina.

## 10. Da confermare con Carlo (la raccomandata per prima)

1. **Il nome del portale.**
   - Raccomandata: **un nome unico del centrale per tutti i clienti** (`portale.<dominio>`), deciso adesso. Esempio:
     se la fetta 0 conferma 0 portali pubblicati, nessun cliente rifà niente oltre al primo login. Un cliente che avesse
     1 passkey e un SSO rifarebbe la passkey (≈ 2 minuti) e il suo IT aggiungerebbe un URI di ritorno.
   - Alternativa: tenere il nome di ogni macchina. Il centrale risponde su 5 nomi più i suoi, ognuno con il suo
     certificato e il suo record DNS, spesso in mano ad altri. Un nome `sslip.io` non si sposta comunque.
2. **Il nome del personale.**
   - Raccomandata: **aperto a internet, con il secondo fattore obbligatorio** e i limiti di 18.6.3. Il portale dei clienti
     è già aperto, sullo stesso processo: chiudere solo il nome del personale toglie poco.
   - Alternativa: solo dagli indirizzi elencati (ufficio, casa di Carlo). Un collega fuori sede passa da `ssh -L` al
     VPS.
3. **REST e MCP dei clienti col centrale fermo.**
   - Raccomandata: **solo dal centrale**. Esempio: un VPS al 99,9% (valore tipico, non misurato) è fermo ≈ 43 minuti al
     mese, più ≈ 20 s per ogni aggiornamento a mano. In quei minuti la CI di un cliente che chiama `/api/v1` fallisce e
     riprova. Il deploy al push no, perché passa dal nome dell'istanza.
   - Alternativa: i token dei clienti validi anche sulla macchina, in sola lettura, sul nome dell'istanza. Costa ≈ 2 g,
     aggiunge un ingresso autenticato su ogni macchina, e un token revocato resta valido lì finché il centrale non
     raggiunge la macchina.
4. **Cosa vede un cliente col VPS spento.**
   - Raccomandata: **nulla risponde sul nome del portale**. La pagina statica di nginx copre aggiornamenti e processo
     caduto, e il monitor esterno avvisa Carlo.
   - Alternativa: una pagina di stato su un'altra infrastruttura (`stato.<dominio>`), comunicata ai clienti. È un servizio
     in più da tenere.
5. **Due persone con lo stesso nome su due macchine.**
   - Raccomandata: **la regola della consegna** (Carlo, 23/11): la seconda arriva come `<nome>.<workspace>`, e lo dice il
     passaggio. Esempio: `mario` su due macchine, una delle due entra come `mario.acme`, stessa password.
   - Alternativa: il passaggio si ferma finché un admin non rinomina a mano.
6. **Quanto resta la copia spenta delle identità sulla macchina.**
   - Raccomandata: **7 giorni**, come la copia ferma di AD-013. Per 7 giorni i segreti stanno in due posti.
   - Alternativa: 30 giorni. Più tempo per tornare indietro, e un mese di segreti in due posti.

### Le risposte di Carlo (24/11/2026)

1. **Nome del portale:** un nome unico del centrale per tutti i clienti (`portale.<dominio>`), la raccomandata.
2. **Nome del personale:** aperto a internet, ma il **secondo fattore è facoltativo, scelto per utente** (contro
   18.6.2, che lo voleva obbligatorio per tutti). 18.6.2 cambia: la politica del secondo fattore del pannello del
   personale è per utente (`second_factor: required | optional` sulla persona, come oggi la politica `admins`/`everybody`
   ma per nome), con i limiti di 18.6.3 sempre attivi. Il rischio resta scritto: una persona del personale senza secondo
   fattore, con la password rubata, raggiunge tutte le macchine. La pagina Utenti lo dice accanto a chi non ce l'ha.
   Il portale dei clienti segue la politica del workspace, come oggi.
3. **Centrale fermo:** REST e MCP dei clienti solo dal centrale; pagina statica di nginx; la CI al push continua sul
   nome dell'istanza (la raccomandata).
4. **Nomi in collisione e copia spenta:** `<nome>.<workspace>` come nella consegna, segreti vecchi cancellati dopo 7
   giorni (la raccomandata).

## 11. Alternative scartate

- **B. Identità nel centrale e copiata su ogni macchina** (login locale col centrale fermo). Vuol dire due copie dei
  segreti su 7 macchine e una revoca che arriva quando la macchina è raggiungibile. È l'opzione C di AD-014, già
  scartata.
- **C. Il centrale provider OIDC, il portale sulle macchine.** Le pagine dei clienti restano sulle macchine, che restano
  pubbliche, e una correzione aspetta l'aggiornamento della macchina. È il contrario del modello che Carlo ha chiesto.
- **D. Inoltrare ai clienti l'HTML delle macchine** (come `/placed`), per anticipare il passaggio prima delle pagine di
  AD-017. Una macchina compromessa scriverebbe script sotto l'origine del portale di tutti i clienti.
- **Spostare anche le sessioni.** Il cookie vale per nome di host e dura al massimo 8 h: si sposterebbero credenziali al
  portatore per evitare un login.
- **Un nome per cliente.** È già scartato in `docs/portale-clienti.md`: un login solo dietro nomi diversi dà
  un'impressione di separazione che non c'è, e ogni nome vuole un record DNS fuori dal nostro controllo.

## Non verificato

- I numeri delle 5 macchine dei clienti: persone, fattori, passkey per RP ID, SSO, token, grant, inviti, collisioni,
  webhook sull'indirizzo del portale. Si misurano alla fetta 0.
- Che oggi nessuna macchina di un cliente abbia un portale pubblicato. Lo dice la memoria del progetto (uno solo
  pubblicato il 21/11, poi tolto), non una lettura di oggi.
- Le Related Origin Requests di WebAuthn (`/.well-known/webauthn`), che permetterebbero di usare un RP ID vecchio da un
  nome nuovo: non controllate nei browser dei clienti, e non le uso.
- Una passkey su lab con il certificato autofirmato, in Chromium: la fetta 3 usa l'autenticatore virtuale di Playwright,
  e se non basta un nome con un certificato vero.
- Che il codice di `/placed` (`app.py` da riga ≈ 20.820) regga tutte le 143 route per un server non spostato: è stato
  scritto per i server spostati. Lo dice il test di parità della fetta 2.
- Che le chiavi SSH dei collegamenti sulle macchine non siano limitate all'indirizzo del PC di Carlo (`from=`): il VPS
  parte da un altro indirizzo.
- Il fornitore e il paese del VPS (UE).
- Il tempo del ripristino del centrale, con le identità di tutti i clienti dentro: nessuna misura.

## Tracciabilità

Chat di Carlo del 24/11/2026 (il portale nel centrale; il modello dei pannelli SaaS di gestione Odoo; il VPS, la
release, il secondo fattore, la riserva). Chiude la domanda di AD-017 §10.2 e la nota di 17.4.1. Vincoli da AD-014 (casa,
ambito, due controlli, verso delle chiamate), AD-013 (7 giorni), AD-008 §9 passi 7-10 (consegna) e
`docs/portale-clienti.md`. Nessun `TS-NNN`, `T<n>`/`HT<n>`.

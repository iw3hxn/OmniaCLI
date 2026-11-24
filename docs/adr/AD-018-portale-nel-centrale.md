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
2. **Il centrale casa** (18.2): W remoti, ambito su ogni route, REST/MCP inoltrati, liste, spostati diretti all'host.
   - *Prova*: la parità del vincolo 3, con 0 differenze. Il token di W su lab3 chiama `/api/v1` di un server su lab4. La
     voce `on_behalf` è nel registro di lab4.
   - *Si annulla*: con il rilascio di prima. Il record di casa su lab4 torna com'era.
   - *Stima*: 5 g.
3. **Nomi e bordo**: due nomi, `portal_admits` nei due versi, RP ID come impostazione, pagina di manutenzione nginx.
   - *Prova*: una passkey (autenticatore virtuale di Playwright) fatta sul nome A vale su A e su un altro nome dello
     stesso RP ID, ed è rifiutata su un nome di un altro dominio. Il personale è rifiutato sul portale e i clienti sul
     pannello, con 403 nel registro. Con OmniaCLI fermato su lab3, nginx risponde con la pagina statica.
   - *Stima*: 2 g.
4. **Webhook della CI sul nome dell'istanza** (18.5.1).
   - *Prova*: un webhook sull'indirizzo del portale viene riscritto sul nome dell'istanza, con lo stesso id. Con lab3
     fermo, un push fa il deploy della test su lab4.
   - *Stima*: 1 g.
5. **Il passaggio di una macchina e il ritorno** (18.3): congelamento, record di casa, login locale spento, inviti per il
   secondo fattore perso, 7 giorni, cancellazione dei segreti, registro unito.
   - *Prova* su lab4:
     - le persone entrano da lab3 con la stessa password e lo stesso TOTP;
     - su lab4 il loro login è rifiutato;
     - chi aveva solo una passkey sul nome di lab4 non può attivare un fattore con la sola password, e lo fa dal link;
     - «Torna sulla macchina» porta indietro una password cambiata su lab3;
     - dopo i 7 giorni (orologio finto) su lab4 ci sono 0 segreti di persone.
   - *Stima*: 3,5 g.
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

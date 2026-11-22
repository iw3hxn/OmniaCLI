# UX-009 — La mappa dell'infrastruttura

Stato: contratto di design, prima della build. File proposto: `/home/carlo/Projects/OmniaCLI/docs/ux/UX-009-mappa-infrastruttura.md`. Lo implementa chi scrive i template di `omniacli/templates/`, con Jinja, JS senza framework e SVG in linea disegnato dal JS. Le classi sono quelle che esistono già: `.tabs`, `.panel`, `.small`, `.mono`, `details`, `.state-dot`, `.badge`, `.steps` di `job.html`. Le stringhe vanno in `omniacli/i18n/it/*.json`.
Valgono le regole comuni e i criteri di UX-001…008, in particolare UX-006 §3 e UX-008 §2 regole 1-3: questo contratto li applica e non li ripete.
Origine: la richiesta di Carlo del 22/11/2026, «fai partire anche un workflow che mostri con degli schemi anche quello che sta succedendo, di come è configurata l'infrastruttura e che l'utente veda graficamente cosa ha configurato». Il contratto fonde due design dello stesso giorno: la mappa della configurazione è la base, e sopra ci stanno lo strato «Adesso» e il percorso di un'istanza. Si appoggia ad AD-003 e AD-004 (`docs/adr/postgresql-della-macchina.md`).

## 0. Cosa c'è oggi (22/11/2026)

Non c'è nessuno schema, nessuna route e nessun template di topologia. Per rispondere a «com'è fatto, e cosa si muove adesso?» oggi servono 6 pagine:

| Fatto | Dove sta oggi | In JSON? |
|---|---|---|
| Istanze, ambienti, stato, `busy`, versione, staging e loro produzione, backup programmato | `/instances`, `/api/instances` (un collegamento per volta), `/api/servers` | sì |
| Collegamenti, indirizzo, tunnel | `/links`, `/machines` (solo admin della macchina) | solo i nomi |
| Workspace: dominio, limiti, membri, certificato, nomi sul web, alias, reindirizzamenti | la pagina del workspace | no (solo il contesto HTML) |
| Dove sta il database (dedicato, cluster, altra macchina, replica) | la riga Database della pagina dell'istanza (`database_line`) | no; `/api/postgres` solo per l'admin, ed è lenta |
| Job in corso con i passi | `/jobs`, `/jobs/<id>` (`.steps`) | `/api/jobs`, senza `started`, `progress`, `from` e `to` |
| Certificati in scadenza, istanza che non risponde | il monitor, che li manda solo per mail o webhook; `Monitor.problems()` non ha chiamanti | no |

## 1. Persone e lavoro primario

| Persona | Chi è nel codice | Cosa vede |
|---|---|---|
| **Integratore, admin della macchina** (Carlo) | `is_admin`, non `scoped` | S1 Macchine e collegamenti, S2 di ogni macchina e di ogni workspace, S3 |
| **Admin di un workspace** (il cliente) | `scoped`, ruolo admin nel workspace | S2 dei suoi workspace e S3 delle sue istanze, con la customer view |
| **Developer** | ruolo `developer` | come l'admin del workspace, con meno fatti (§7); D2 |
| **Supporto** | ruolo `user` | come il developer, senza i backup e senza i numeri del monitor (§7); D2 |

Device: desktop 1440×900. Il telefono (375×812) serve in sola lettura, per «c'è qualcosa che non va?».

**Lavoro primario**: in 5 secondi capire «tutto a posto» oppure «ecco le N cose da guardare», e vedere dove sta ogni cosa configurata. Lavoro secondario: seguire cosa si muove adesso (un job, un trasferimento).

| Schermata | Domanda a cui risponde | Primario |
|---|---|---|
| `/instances`, vista **Mappa** | «Com'è fatta la mia infrastruttura, va tutto bene, cosa si muove?» | **nessuno**: la mappa non crea niente (UX-008 regola 1) |
| Pannello **Percorso dell'istanza** (S3) | «Da dove arriva una richiesta, dove va il database, dove va la posta?» | nessuno |

## 2. Regole che UX-009 aggiunge a quelle comuni

1. **La mappa legge e basta.** Non ha nessun pulsante che cambi qualcosa: Ferma, Riavvia, Ripristina e Nuova staging restano nelle loro pagine, e la mappa ci porta con un link «Apri…». È la prevenzione dell'errore più forte che c'è.
2. **Una mappa sola e tre schemi.** S1 Macchine e collegamenti, S2 La macchina o il workspace, S3 Il percorso di un'istanza. Lo strato «Adesso» (stato, ⟳, trasferimenti) si disegna **sugli stessi nodi**: non è un quarto schema.
3. **Un colore non è mai da solo.** Ogni stato ha il suo segno e la sua parola (§6).
4. **Una macchina che non risponde diventa un riquadro con il motivo** (UX-008 regola 2), e le altre si disegnano lo stesso.
5. **Un numero in testa è un filtro** (UX-008 regola 3). Il clic accende sulla mappa esattamente quei nodi.
6. **Il filtro di chi vede cosa sta nel server**, dentro la route: `visible_servers`, `customer_view` e il ruolo nel workspace. Il JS non nasconde niente che il server abbia mandato.

## 3. Dove vive (nessuna voce di menu nuova: D1)

- **`/instances` ha un selettore di vista «Elenco | Mappa»**, sopra la tabella. È lo schema di elenco e kanban di Odoo: stessi dati in un'altra forma (Jakob). `/instances` sta già in `WORKSPACE_ROUTES` e ha già il filtro `visible_servers`, quindi la mappa arriva a ogni persona che vede l'elenco, con il perimetro che ha già.
  - La vista Elenco tiene il suo primario «+ Nuova istanza». La vista Mappa non ha primario.
  - La scelta va nell'URL (`?view=map`) e si ricorda nel browser.
  - `title` di «Mappa»: «Mostra le stesse istanze come schema: dove sta ogni cosa configurata e cosa si muove adesso. Non cambia niente.»
- **Pagina del workspace**: nella testa della scheda Istanze c'è un link (non un pulsante) «Sulla mappa», che porta a `/instances?view=map&workspace=<ws>`. Le schede restano 4.
- **Pagina dell'istanza**: sotto la tabella della scheda Ambienti c'è un link `.small` «Sulla mappa», che porta a `…&focus=<server>` con S3 già aperto. Le schede restano 5 (UX-006 §2).
- **Sotto `/links/<nome>/`** la mappa mostra solo quella macchina: si apre su S2, senza S1.

## 4. La pagina, dall'alto (vista Mappa)

1. **Testata con il verdetto e i numeri**:
   - il verdetto «✓ Tutto a posto» oppure «⚠ 3 cose da guardare»;
   - i conteggi «In corso N · In coda N · Stanotte: 14 backup riusciti, 1 fallito». Ogni numero è un filtro (regola 5). Lo user non vede i backup;
   - «Letta alle gg/mm/aaaa hh:mm» (`whenText`), «si aggiorna da sola».
2. **Legenda**: una riga sopra lo schema, mai sovrapposta. Mostra **solo i segni presenti** nello schema aperto, e ogni voce ha un `title`. Sul telefono è un `<details>` «Legenda» chiuso.
3. **Striscia «In corso»**: i job running e queued, uno per riga, al massimo 5, poi «e altri N», con un link a `/jobs`.
   - Ogni riga ha il titolo, chi l'ha avviato e da quando. È un link alla pagina del job, dove si ferma.
   - «Da quando» e i 2 minuti di un job finito si contano sull'orologio del server (`age_s`, `ended_s` nella risposta), mai su quello del browser: chi legge da un altro fuso vede lo stesso numero.
   - Una staging fatta su un'altra macchina è una riga sola: il job di là (`follows`) sta nella riga del job di qua, che ne mostra già il passo.
   - Un job con `progress` mostra i passi come flusso (Backup → Trasferimento → Staging), con il componente `.steps` di `job.html` in versione compatta: stesse classi.
   - Un job senza `progress` è una riga sola: «⟳ in corso da 3 min».
   - Un job finito resta 2 minuti con il suo esito (✓ / ⚠ / ✕), poi esce. Se l'esito è ✕ o ⚠, passa a «Da guardare» per 24 ore.
   - È l'unica zona con `aria-live="polite"`.
   - Senza job: «Niente in corso. Ultima azione: <titolo>, <esito>, alle gg/mm/aaaa hh:mm.»
4. **Briciole**: «Macchine › lab2 › Workspace cliente», ognuna con un link. Indietro nel browser torna al livello prima (lo stato sta nell'URL: `machine=`, `workspace=`, `focus=`).
5. **Lo schema** (S1 o S2, §5) e il pannello S3 a destra quando è aperto.
6. **«Lo schema in elenco (N righe)»**: l'alternativa testuale (§10).

## 5. I tre schemi

### S1 — Macchine e collegamenti (solo l'admin della macchina, solo sul pannello centrale)

Si disegna solo con almeno 1 collegamento. Con 0 collegamenti la mappa si apre direttamente su S2 di questa macchina.

**Nodi**:
- **Macchina**: un riquadro per questa macchina, che sta per prima con la scritta «questa», e uno per ogni collegamento. Sul riquadro:
  - il nome del collegamento, e il nome della macchina in `.small` se è diverso;
  - la versione di OmniaCLI;
  - CPU % e RAM % di adesso, e lo spazio libero su disco;
  - il nodo del **cluster PostgreSQL** con la versione, solo se il cluster è stato creato.
- **Workspace**: un chip dentro la macchina, con l'etichetta, «N istanze · M staging» (le staging si contano a parte, ovunque stia la loro produzione, come fa l'Elenco), il segno peggiore dei suoi ambienti con la sua parola, ⟳ se uno è occupato, e la barra del disco (usato su limite). Le istanze fuori dai workspace stanno in un chip grigio «Fuori dai workspace», solo se ce ne sono.
- **Database su un'altra macchina**: un riquadro tratteggiato **sotto la macchina che lo usa**, con `host:port` e «N ambienti». Se c'è la replica, un secondo riquadro «replica» sotto. Le linee sono un albero che scende dal riquadro della macchina, un tronco per ognuno: nessuna attraversa il riquadro di un'altra macchina.
- **Macchina da aggiornare**: una macchina collegata che risponde ma ha un OmniaCLI senza la mappa non è una macchina giù. Ha il segno ↑, la parola «da aggiornare» in `var(--info)`, il riquadro puntinato e la linea del collegamento continua. Non si conta fra quelle che non rispondono.

**Linee**. Ciò che è normale non ha linea: un database dedicato è un'icona ⛁, non una linea.

| Linea | Tratto | Da → a |
|---|---|---|
| Collegamento | continua | questa → macchina collegata, sopra la riga delle macchine; verso un'altra riga scende nello spazio accanto alla colonna, senza attraversare riquadri. Se il collegamento non risponde: tratteggiata, con ✕ |
| Staging su un'altra macchina | a trattini, con freccia ed etichetta «2 staging» | dal bordo del riquadro della macchina, all'altezza del chip del workspace della produzione, al bordo del riquadro dell'altra all'altezza del chip là. L'etichetta sta nello spazio fra i due riquadri, disegnata sopra i nodi |
| Dati in movimento | quella della staging, che scorre (il motivo di `.step-wire.flowing`, in `var(--warn)`), con l'etichetta «Trasferimento 42 % · 1,2 GB» | solo mentre il job è al passo di trasferimento |
| Database su un'altra macchina | a punto e linea; «3 ambienti» sta nel riquadro del database | macchina → riquadro del database, sotto di lei |
| Replica | puntinata | macchina → riquadro della replica |

**Clic**:
- sul riquadro della macchina si apre S2 di quella macchina;
- sul chip del workspace si apre S2 di quel workspace;
- sul database o sulla replica si apre il pannello con `host:port`, gli ambienti che lo usano e la replica.

### S2 — La macchina o il workspace (tutte le persone, ognuna con il suo perimetro)

È un flusso da sinistra a destra in colonne. Per l'admin della macchina la radice è la macchina, con un riquadro per ogni workspace. Per chi è `scoped` la radice è il suo workspace, o un riquadro per ognuno dei suoi workspace. Il nodo della macchina non c'è.

1. **Nomi sul web**:
   - ogni nome è una pillola, con il lucchetto e la parola: «HTTPS» / «senza HTTPS» / «DNS non punta qui»;
   - un alias è una pillola normale; un reindirizzamento è una pillola in corsivo con «→ nome»;
   - la colonna c'è **solo quando lo schema mostra un workspace solo**. Con più workspace, sul nodo dell'ambiente resta il conteggio «3 nomi», con la rete del §8.
2. **Ambienti**: ogni istanza è un nodo di produzione con il nome, la versione maggiore, il primo indirizzo e il segno di stato con la sua parola.
   - Il **test è una pastiglia dentro il nodo**, con il suo stato.
   - Se l'istanza è occupata, sotto il nome c'è «⟳ <titolo breve del job>».
   - Il nodo porta tre marche:
     - **backup** di stanotte: «✓ 02:10» / «✕ backup fallito gg/mm/aaaa hh:mm» / «⚠ nessun backup programmato»;
     - **monitor**: «non risponde dalle hh:mm», solo quando l'istanza è giù;
     - **certificato**: «certificato: 9 giorni», solo sotto i 14.
   - Le **staging** sono nodi piccoli appesi alla produzione, con la linea tratteggiata «copia del gg/mm», lo stato e «✉ 12» se catturano la posta.
     - Una staging ferma ha il segno «ferma per scelta» e non è un allarme.
     - Una staging su un'altra macchina è un nodo al bordo del riquadro: «su lab2» per l'admin della macchina, «su un'altra macchina» per gli altri.
3. **Database**: «dedicato» non ha linea (⛁ nel nodo). Il database condiviso della macchina è un nodo «PostgreSQL 17 della macchina», con le linee che ci arrivano. Un database esterno è il nodo `host:port`, più quello della replica. Ogni nodo dice quanti ambienti lo usano.
4. **Destinazioni dei backup**: ci sono solo se almeno un backup automatico manda le copie a una destinazione. Stanno **nella stessa colonna di destra, sotto i database**, con la loro intestazione. La linea va dalla produzione alla destinazione, con l'orario accanto al nodo quando è lo stesso per tutte. Lo user non la vede.

**Le linee verso la colonna di destra** non si sovrappongono e non attraversano nodi: ogni database e ogni destinazione ha una sua corsia nello spazio prima della colonna. Con un workspace solo parte una linea da ogni ambiente. Con più workspace parte una linea per workspace e bersaglio: dal fianco del riquadro se a destra non c'è niente, altrimenti dall'alto, lungo lo spazio sopra la sua riga.

**Clic su un ambiente**: si apre S3 nel pannello. Sul riquadro del workspace si apre la pagina del workspace, solo per chi la può aprire (l'admin). Per developer e user il riquadro non è un link e non ne ha l'aspetto.

### S3 — Il percorso di un'istanza (pannello laterale)

Il pannello è largo 420 px sul desktop. Sul telefono è un foglio a tutto schermo. Esc lo chiude, e il focus torna al nodo.

**Il percorso di una richiesta**: Browser → Nomi → Server web → Odoo → Database (→ Replica). È in verticale sul telefono, e anche nel pannello di 420 px quando una casella affiancata sarebbe più stretta di 104 px: le parole si leggono intere.
- **Nomi**: il principale, poi gli alias e i reindirizzamenti come nodi piccoli con la freccia «→ principale». Ognuno ha il lucchetto con la parola: «HTTPS, 61 giorni» / «senza HTTPS» / «certificato tra 9 giorni».
- **Server web**: il nome della macchina per l'admin della macchina, «il server» per gli altri.
- **Odoo**: lo stato di produzione e di test.
  - Il monitor («su da hh:mm, ultima risposta 180 ms») si vede da developer in su.
  - Il carico degli ultimi 10 minuti sta accanto alla linea, come numero.
- **Database**: dice dove sta («Dedicato», «Cluster della macchina (PostgreSQL 17)», «Su un'altra macchina: host:port»), con lo stesso perimetro di `database_line` (D3).
- **Replica**: «solo lettura · host:port», solo se c'è.

**Il percorso della posta**, solo per le staging che catturano:
- Odoo della staging → «Posta catturata: 12 messaggi, l'ultimo alle hh:mm», oppure «l'ultimo il gg/mm/aaaa hh:mm» quando non è di oggi;
- una linea interrotta verso «Internet», con «non esce: il firewall blocca le porte della posta»;
- se la ricezione è accesa, «Caselle di <dominio>.test → Odoo».

Per la produzione la posta non si disegna.

**Fatti in testo**: il backup automatico (orario, ultimo esito con la data, destinazione) e le staging.
**Link in fondo**: «Apri la pagina dell'istanza», «Apri Odoo», «Apri la Posta» (solo per le staging che catturano) e «Chiudi». Ognuno ha un `title`. «Apri Odoo» c'è solo se l'istanza ha un indirizzo che il browser di chi legge può aprire: un'istanza senza nome risponde su una porta della sua macchina (`localhost:20004`), che sul PC di chi legge è un'altra cosa.

## 6. Stati, colori, segni

| Stato | Colore (variabili di `base.html`) | Segno | Parola |
|---|---|---|---|
| In funzione | `var(--ok)` | ● pieno | «in funzione» |
| Ferma, produzione | `var(--err)` | ■ | «ferma» |
| Ferma per scelta (test, staging) | `var(--muted)` | ○ vuoto | «ferma» |
| Non letto | `var(--muted)` | ? | «stato non letto» |
| Occupata | `var(--info)` | ⟳, ferma con `prefers-reduced-motion` | «in corso: <titolo>» |
| In coda | `var(--info)`, contorno tratteggiato | ⏳ | «in coda» |
| Fatto / con avvisi / fallito | `--ok` / `--warn` / `--err` | ✓ / ⚠ / ✕ | «fatto» / «fatto con avvisi» / «fallito» |
| Macchina che non risponde | `var(--warn)`, riquadro tratteggiato | ✕ | «<nome> non risponde: <motivo>. Le sue istanze qui non ci sono.» |
| Macchina da aggiornare (risponde, senza la mappa) | `var(--info)`, riquadro puntinato, collegamento continuo | ↑ | «da aggiornare» · «OmniaCLI là è più vecchio: aggiornalo per vederla sulla mappa.» |
| Nome non ancora letto | `var(--muted)` | lucchetto grigio | «HTTPS non ancora letto», con la sua voce in legenda |
| Nome senza HTTPS o senza DNS | `var(--warn)` | ⚠ | «senza HTTPS», «DNS non punta qui» |

- Riempimenti e tratti dello SVG usano solo `var(--…)`, mai `#` o `rgb(`: il tema scuro segue da solo. `.state-dot` e `.badge ok/warn/error` si riusano.
- Il contrasto è almeno 4,5:1 sul testo e 3:1 sulle linee, nei due temi. I testi grigi sul fondo della pagina (intestazioni di colonna, etichette delle linee, «Letta alle…», legenda, briciole) usano `--map-ink`, il `--muted` del tema mescolato con il colore del testo: il `--muted` da solo sul fondo chiaro è a 4,47:1. I contorni dei riquadri e dei nodi usano `var(--muted)`.

## 7. Visibilità

| Elemento | admin macchina | admin workspace | developer | user | Meccanismo |
|---|---|---|---|---|---|
| Vista Mappa in `/instances` | sì | sì, le sue istanze | sì (D2) | sì (D2) | `visible_servers` |
| S1, nodi macchina, carico della macchina, nomi dei collegamenti | sì | — | — | — | non `scoped`, pannello centrale |
| Nomi di macchine, host, percorsi, altri workspace | sì | — | — | — | `customer_view` nella route |
| Riquadro del workspace cliccabile | sì | sì | — | — | ruolo admin nel workspace |
| Stato, occupata, in coda, striscia In corso | sì | sì | sì | sì | `/api/jobs` filtrato come oggi; il «where» dei passi diventa «il server» |
| Marche di backup, destinazioni, «Stanotte» | sì | sì | sì | — | oggi per vedere i backup serve developer |
| Monitor (numeri) | sì | sì | sì | — | uptime da developer in su |
| Monitor «non risponde», certificato < 14 giorni | sì | sì | sì | sì | — |
| Conteggio della posta catturata | sì | sì | sì | sì | come la scheda Posta (UX-008 §3) |
| `host:port` del database esterno e della replica | sì | come la riga Database di oggi (D3) | idem | idem | lo stesso perimetro di `database_line` |
| Staging del cliente su un'altra macchina | con il suo nome e la macchina | «Staging del gg/mm», o il suo nome sul web se ne ha uno; «su un'altra macchina»; nessun indirizzo `localhost` | idem | idem | la macchina la chiede al collegamento come `map`, mai con il nome del cliente (`stagings_of`) |
| Nome di questa macchina in `/instances`, `/backups`, `/api/instances`, `/api/backups` | sì | — | — | — | `shown_name`: vuoto per lo `scoped` |
| Varco nel firewall | DEFER | — | — | — | §8 |

## 8. HIDE / DEFER

| Cosa | Dove sta | Rete di sicurezza |
|---|---|---|
| Nomi sul web, con più di un workspace a schermo | conteggio «3 nomi» sul nodo; elenco intero in S3 | ⚠ sul nodo se un nome è senza HTTPS o senza DNS |
| Il test come nodo a parte | pastiglia nel nodo | la pastiglia mostra lo stato e ⟳ |
| Staging oltre la seconda, per produzione | «+N staging», che si apre al clic | una staging occupata o con un problema resta sempre visibile |
| Istanze oltre la sesta in un riquadro di workspace | «+N altre», che apre il riquadro | un'istanza ferma in produzione, occupata o con una marca ✕ resta visibile |
| Limiti, membri, certificato, indirizzi dei collegamenti, numeri del monitor | nel pannello | — |
| Job conclusi | escono dopo 2 minuti | ✕ e ⚠ restano 24 ore in «Da guardare» |
| Bozze di server | una riga «N bozze, non nella mappa» con il link a `/config`, solo per l'admin | la riga compare solo se ce ne sono |
| Tunnel aperti, gruppi della Dashboard, relay, publications | fuori dalla mappa | — |
| Animazioni | spente con `prefers-reduced-motion` | restano i segni e le percentuali |
| Firewall dei workspace | **DEFER**: non esiste una lettura senza scrittura. Non si chiamano mai le azioni on/apply/off dell'helper per leggerlo | in S3, quando il dato esisterà: «varco nel firewall: solo host:port», solo per l'admin della macchina |

## 9. Vuoto, dati densi, attesa

- **Vuoto, admin della macchina**: un riquadro di questa macchina con «Nessuna istanza ancora: si crea dalla vista Elenco» (link) e, senza collegamenti, «Un'altra macchina si collega da Macchine › + Aggiungi un server» (link). Il primario non si duplica nella vista Mappa.
- **Vuoto, workspace**: «Il workspace non ha ancora istanze», con un link alla vista Elenco.
- **Dati parziali**: una fonte che non risponde (un collegamento, i backup, la posta) mette «— non letto: <motivo>» al suo posto e non blocca il resto.
- **Dati densi**:
  - 3 macchine e 60 istanze stanno in 1440 px senza scorrere in orizzontale: in S1 le macchine vanno a capo, in S2 i riquadri dei workspace stanno a griglia;
  - oltre 30 nodi c'è un selettore «Mostra: tutto | solo da guardare e in corso», che parte su «tutto»;
  - oltre 8 istanze c'è la ricerca di `_list.html`;
  - i nomi lunghi si troncano con «…», e il nome intero sta nel `title`.
- **Attesa**:
  - prima dei dati si vede lo scheletro dei riquadri, mai uno spazio bianco;
  - lo schema compare in **< 2 s** con le macchine che rispondono;
  - un collegamento muto diventa il suo riquadro dopo al massimo 5 s (`LIST_TIMEOUT`).

## 10. Telefono (fino a 700 px) e alternativa testuale

- **In verticale**:
  - S1: le macchine una sotto l'altra, e i collegamenti come connettore verticale da «questa»;
  - S2: le colonne diventano righe (Nomi ↓ Ambienti ↓ Database ↓ Destinazioni);
  - S3 e i passi dei job vanno in verticale, come `job.html` già fa a 640 px.
  Niente scorrimento orizzontale, e nodi tattili alti almeno 44 px.
- **Alternativa testuale**: un `<details>` «Lo schema in elenco (N righe)», chiuso sul desktop e aperto sul telefono. L'elenco non piega niente: nomina anche le staging e le istanze che lo schema raccoglie in «+N».
  - Per S1 e S2 è un elenco annidato con gli stessi fatti: macchina › workspace › istanza › ambiente, con stato, database, nomi, backup, staging e la loro macchina, e il job in corso. Per esempio: «erp-cliente: in funzione · backup di stanotte riuscito alle 02:10 · in corso: Nuova staging, passo 2 di 3, Trasferimento 42 %».
  - Per S3 è un elenco ordinato: «1. Browser 2. erp.cliente.it, HTTPS, 61 giorni 3. …».
- **Accessibilità**:
  - ogni SVG ha `role="img"`, un `<title>` che riassume **lo schema disegnato** (S1: «2 macchine, 4 workspace, 8 istanze · 8 staging; 1 macchina non risponde»; una macchina: «lab1: 2 workspace, 6 istanze · 4 staging»; un workspace: «Workspace acme: 5 istanze · 4 staging»), con il singolare dove serve, e `aria-describedby` verso il suo elenco;
  - i nodi hanno `tabindex="0"`, e Invio apre il loro clic.

## 11. Aggiornamento dal vivo

| Cosa | Ogni quanto | Come |
|---|---|---|
| Lo strato «Adesso»: verdetto, job, occupati, carico | 5 s | `GET /api/map?part=now`, letto in memoria. Il server lo chiede ai collegamenti con `LIST_TIMEOUT` |
| La mappa intera: struttura, stati, backup, monitor, certificati | 60 s, e subito quando cambia l'insieme degli occupati o quando finisce un job che la lettura intera non aveva (un job di 2 s nasce e muore fra due letture di «Adesso») | `GET /api/map` |
| Un collegamento che non ha risposto | non viene richiesto da «Adesso» per 90 s: risponde subito con l'ultimo motivo | lo richiede la lettura intera |
| Conteggio della posta catturata | al massimo ogni 5 minuti | cache nel server, solo per le staging che catturano; il nodo dice «alle hh:mm» |
| Certificati | il controllo giornaliero del monitor | il nodo porta la data del controllo |
| Scheda del browser nascosta | — | nessuna lettura (`document.hidden`); al ritorno, una lettura intera |
| **Mai, dalla mappa** | — | `/production/mail`, `/mail/inbox` (avvia una odoo shell), `/{role}/database`, `/api/postgres`, `/api/backups`, `/pitr`, `/remote-backups`, `/addresses/check`, `/api/access/verify`, `POST /api/links/{name}/test` |

- Una lettura fallita non cancella lo schema: lo schema resta, attenuato, con la fascia «Non aggiornata dalle gg/mm/aaaa hh:mm: <motivo>. Riprovo fra 5 s.»
- Una lettura che non arriva in 15 s è una lettura fallita (`AbortController`), con il motivo in italiano.
- Ogni domanda a un collegamento ha 5 s **in tutto** (`LIST_TIMEOUT` attorno all'intera richiesta, non per fase): un collegamento che risponde un byte alla volta non trattiene la mappa. I collegamenti si chiedono mentre questa macchina legge i suoi stati.
- Alla fine di un job, il nodo nuovo (per esempio una staging) compare senza ricaricare la pagina.

## 12. Il necessario

- **Stato del sistema**: il verdetto, «Letta alle…», la fascia «Non aggiornata», la striscia «In corso», il riquadro di ogni macchina che non risponde.
- **Annulla**: non applicabile, perché la mappa non agisce. Un job si ferma dalla sua pagina, a un clic dalla riga della striscia. Il pannello si chiude con Esc o con «Chiudi».
- **Prevenzione dell'errore**: non applicabile, perché la mappa non avvia niente. Il cliente non arriva ai dati di altri, perché il filtro sta nel server.
- **Messaggio d'errore**: ogni fonte illeggibile dice cosa e perché, in italiano. I casi comuni di un collegamento hanno la loro frase (`links.describe_error`): «nessuna risposta in tempo», «connessione non riuscita: …», «ha chiuso la connessione senza rispondere: OmniaCLI là non è in esecuzione, o si sta avviando», «la connessione si è interrotta prima che la risposta fosse intera». Nel browser: «il pannello non risponde (la rete, o si sta riavviando)», «nessuna risposta in 15 s».
  - All'admin della macchina dice anche il rimedio: «controlla il collegamento in Collegamenti».
  - Un workspace non suo risponde 404, senza dire se esiste.
  - Una macchina collegata con un OmniaCLI senza la mappa diventa il riquadro «OmniaCLI là è più vecchio: aggiornalo per vederla sulla mappa».
- **Aiuto**: la legenda, e un `title` su ogni nodo, linea, conteggio, voce della legenda, `summary`, selettore e link del pannello, scritto da quello che il codice mostra o apre.

## 13. Contratto dei dati (una route, una forma JSON; i nomi definitivi li decide chi implementa)

Una sola route nuova, `GET /api/map`:
- è additiva: niente di esistente cambia nome;
- va aggiunta a `WORKSPACE_ROUTES`;
- in `audit.py` va fra le **NOT_SENSITIVE**, come `/api/servers` e `/api/instances`, di cui porta lo stesso genere di fatti. Non porta elenchi di backup, testi di posta né log: della posta dà solo il conteggio;
- il filtro di visibilità, `customer_view` e il ruolo si applicano dentro la route;
- verso i collegamenti la route chiede `/api/map` alla macchina collegata, con il ruolo del collegamento limitato da quello di chi chiede (`X-OmniaCLI-Role-Cap`), e con `LIST_TIMEOUT` di 5 s.

Parametri: `part=all|now`, `machine=<id>`, `workspace=<ws>`, `focus=<id>` (che aggiunge `flow`), `stagings_of=<nome di una macchina>`.

`stagings_of` è la domanda che una macchina fa a un collegamento per i suoi utenti dei workspace: risponde solo le staging fatte qui delle produzioni di quella macchina (`machines[0].workspaces[].instances`), senza job, verdetto, altri workspace. La fa come `map` (`X-OmniaCLI-User`) e con il ruolo `user`: nel registro degli accessi della macchina collegata non compare mai il nome di un cliente. Uno `scoped` che la passa riceve la sua mappa di sempre.

```
{ "at": iso, "part": "all|now",
  "viewer": {"scope": "machine|workspace", "role": "admin|developer|user"},
  "verdict": {"ok": bool, "watch": [{"kind": "stopped|down|backup_failed|job_failed|job_warned|certificate|machine_unread|machine_older|names|disk",
                                     "node": id, "text": "<tradotto>", "since": iso}]},
  "tonight": {"ok": n, "failed": n, "unscheduled": n} | null,              // null per lo user
  "machines": [{ "id", "here": bool, "name": str|null, "machine_name": here_name|null,   // null allo scoped
     "answered": bool, "error": str|null, "older": bool, "read_at": iso, "version": str,
     "load": {"cpu","ram"}|null, "disk": {"free","total"}|null,           // solo admin della macchina
     "cluster": [{"major","running"}], "drafts": n|null,
     "workspaces": [{ "id","name","label","domain","certificate": "letsencrypt|own|",
        "can_open": bool, "disk": {"used","size"}|null, "limits": {…}|null, "members": n|null,
        "instances": [{ "id": "<machine>/<server>", "server", "type": "production|staging",
           "parent": id|null, "elsewhere": bool, "version", "created", "url", "page",
           "envs": {"production": {"state": "running|stopped|unknown", "busy": bool, "job": id|null,
                                   "names": [{"name","kind": "main|alias|redirect","to": str|null,
                                              "https": bool|null,"dns": "here|elsewhere|none"|null}],
                                   "database": {"where": "dedicated|machine|remote|system",
                                                "major": str|null,"host": str|null,"replica": str|null}},
                    "test": {…}|null},
           "backup": {"scheduled","time","destination": str|null,"last": {"time","ok","job"}|null}|null,
           "monitor": {"state","since","last_ms": n|null}|null,
           "certificate": {"days","checked"}|null,
           "mail": {"capture","count": n|null,"counted_at": iso|null,"inbox": bool}|null }] }],
     "loose": [ istanze fuori dai workspace, stessa forma; solo admin della macchina ] }],
  "edges": [{"from": id, "to": id, "kind": "link|staging|database|replica|backup", "count": n}],
  "destinations": [{"id","label","workspace"}],
  "jobs": [{"id","title","status","outcome","by","created","started","finished","instances": [id],
            "age_s": n|null, "ended_s": n|null,            // secondi dall'avvio e dalla fine, sull'orologio del server
            "follows": "<collegamento>:<job>"|null,          // il job di là che questo aspetta: una riga sola
            "from": id|null, "to": id|null,
            "progress": {"steps": [{"id","label","where": id|"server"}],"step","done","total"}|null, "page"}],
  "flow": { "names": […], "web": {"label"}, "odoo": {"state","monitor","load": [{"t","cpu","mem"}]},
            "database": {…}, "firewall": null,
            "mail": {"capture","guarded","count","last","inbox","domain"}|null } }   // solo con focus
```

Con `part=now` la risposta porta solo `at`, `verdict`, `jobs`, il `busy` e il `job` di ogni ambiente, e `load`.

**Dati che oggi mancano**, e che la route deve aggiungere:
1. `here_name` di ogni macchina (`machine_name`), per legare `parent_machine` al collegamento: oggi una staging su B con la produzione su A non si lega al collegamento A.
2. La posizione del database e la replica in JSON da `database_line`, **non** da `pgreport.where()`, che sbaglia sul database remoto.
3. Lo stato HTTPS e DNS di ogni nome, salvato all'ultima pubblicazione dei nomi. `null` vuol dire «non ancora letto», e si mostra come tale, mai come errore. Un workspace pubblicato prima che la mappa esistesse non ha quel dato: la prima volta si legge il suo sito nginx (`/etc/nginx/conf.d/omniacli-<workspace>.conf`, leggibile da tutti), che dice per ogni nome se risponde in HTTPS; il DNS resta `null` fino alla prossima pubblicazione. Il certificato letto dal monitor **non** si usa per questo: lo chiede a nginx su 127.0.0.1, che per un nome senza HTTPS risponde con il certificato di un altro sito.
4. Il monitor salva `days` e `checked` per ogni certificato, non solo il testo del problema sotto i 14 giorni.
5. I job portano `started`, `progress`, `from` e `to`.
6. La cache del conteggio della posta catturata. Il firewall resta DEFER.

## 14. Criteri di accettazione (Playwright, solo su lab1 172.30.0.11 e lab2 172.30.0.12)

Le prove si fanno come **admin della macchina**, **admin di un workspace**, **developer** e **user**, senza il gruppo developer per le schermate del cliente e del supporto. Viewport 1440×900 e 375×812, lingua IT, tema chiaro e scuro.

Dati:
- lab1 collegata a lab2;
- 2 workspace su lab1, 1 su lab2;
- 1 produzione con database esterno e replica, 1 sul cluster, le altre dedicate;
- 1 backup automatico fallito stanotte;
- 1 staging che cattura la posta.

1. **Dove**: `/instances` ha il selettore «Elenco | Mappa», con i `title`. Nella vista Mappa `.primary:visible` = 0 per ogni ruolo. Il menu ha lo stesso numero di voci di prima.
2. **S1**: i riquadri delle macchine sono 1 + il numero dei collegamenti (2). La somma dei «N istanze» di ogni macchina è uguale alle righe di produzione di `/instances` per quella macchina, e la somma degli «M staging» alle sue righe di staging.
3. **Macchina che non risponde**: con il pannello di lab2 fermo, entro 7 s il riquadro di lab2 contiene «non risponde» e il motivo, e i nodi di lab1 restano tutti. Riacceso lab2, entro 65 s i suoi nodi ritornano.
4. **Staging su un'altra macchina, dal vivo**: si crea su lab2 una staging di una produzione di lab1.
   - Entro 6 s la striscia ha una riga con 3 `[data-step]`, e il nodo della produzione ha `data-state=busy` e la parola «in corso».
   - Al passo di trasferimento la linea fra le due macchine ha la classe che scorre.
   - A fine job il nodo della staging compare senza ricaricare. Fra quei due chip c'è esattamente 1 linea di staging, e l'elenco testuale nomina la staging con la sua macchina.
5. **Database in S2**: l'ambiente con il database esterno e la replica ha 2 linee, e S3 mostra i due `host:port`. Quello sul cluster ha 1 linea. Uno dedicato ha 0 linee e l'icona ⛁.
6. **Il cliente vede solo il suo**: come admin del workspace e come user, il testo del DOM e le risposte di `/api/map` contengono 0 occorrenze di «lab1», «lab2», dei nomi dei collegamenti, di «/var/», «/srv/» e dei nomi degli altri workspace. S1 non c'è. Con `workspace=` di un altro, la risposta è 404.
7. **Ruoli**:
   - lo user ha 0 marche di backup, 0 numeri del monitor e nessun «Stanotte»;
   - per developer e user il riquadro del workspace non ha `href` né `role=button`;
   - la staging su lab2 è «su un'altra macchina» per chiunque non sia admin della macchina.
8. **Titoli e dialoghi**: ogni `button`, `summary`, `[role=tab]`, `[data-node]` e voce della legenda ha un `title` non vuoto e diverso dal testo. Le chiamate ad `alert`, `confirm` e `prompt` sono 0. Ogni data a schermo rispetta `\d{2}/\d{2}/\d{4} \d{2}:\d{2}`.
9. **Colore e legenda**:
   - ogni `[data-node]` contiene una parola di stato del §6;
   - nessun `fill` o `stroke` dello SVG contiene `#` o `rgb(`;
   - il contrasto è di almeno 4,5:1 sul testo e 3:1 sulle linee, nei due temi;
   - senza database esterni la legenda non ha la voce «a punto e linea».
10. **Un numero è un filtro**: con il backup fallito su lab1, la testata dice «1 fallito», il clic accende esattamente 1 nodo, e `/backups` mostra la stessa riga.
11. **Rete**:
    - con 3 minuti di pagina aperta, `/api/map` intera parte al massimo 4 volte e `part=now` fra 30 e 40 volte;
    - le richieste agli endpoint «Mai» del §11 sono 0;
    - con la scheda nascosta, 0 richieste in 30 s;
    - con le due macchine che rispondono lo schema compare in < 2 s, e prima si vede lo scheletro.
12. **Telefono**:
    - a 375 px `document.documentElement.scrollWidth` ≤ 375, e ogni nodo è alto almeno 44 px;
    - il `<details>` dell'elenco è aperto, e i suoi `li` sono almeno tanti quanti i nodi dello schema: nomina anche ciò che lo schema piega in «+N»;
    - S3 è un foglio a tutto schermo, in verticale.
13. **Tastiera, pannello, movimento**:
    - Tab arriva ai nodi, Invio apre S3 con «Apri la pagina dell'istanza», Esc lo chiude e il focus torna al nodo;
    - `…&focus=<server>` apre S3 già aperto;
    - con `reducedMotion: "reduce"`, `document.getAnimations().length` = 0 e la percentuale del trasferimento si legge lo stesso.
14. **Lettura fallita**: bloccando `/api/map` dopo la prima lettura, lo schema resta, attenuato, con la fascia «Non aggiornata dalle …: <motivo>».

## 15. Smell che fanno FAIL (per `odoo-ux-tester`)

- Un colore senza segno e senza parola.
- Una staging o un test fermi segnati come allarme.
- Una macchina muta che sparisce invece di diventare un riquadro con il motivo.
- Uno `scoped` che vede il nome di una macchina, di un collegamento o di un altro workspace.
- Un `.primary`, o un pulsante che agisce, nella vista Mappa.
- Un quarto schema o una voce di menu nuova.
- Una lettura degli endpoint «Mai».
- Scorrimento orizzontale a 375 px.
- Gli screenshot di accettazione presi solo come admin della macchina, o con il gruppo developer.

## 16. Decisioni di Carlo (la raccomandata per prima)

1. **D1 — Dove sta la mappa.**
   - **Raccomandata: il selettore «Elenco | Mappa» in `/instances`.** Sono 0 voci di menu e 0 pagine nuove, e la mappa arriva a ogni ruolo che vede già l'elenco, con lo stesso perimetro.
   - Alternativa: una voce di menu «Mappa» dopo «Istanze», cioè una voce in più per tutti.
   - Alternativa: il selettore in `/machines`. Lì vede solo l'admin della macchina, e i clienti resterebbero senza mappa.
2. **D2 — Developer e user vedono la mappa ridotta (§7).**
   - **Raccomandata: sì.** Per il supporto è la risposta a «gira?», e i dati sono quelli che `/instances` già mostra loro.
   - Alternativa: solo l'admin del workspace e l'admin della macchina.
3. **D3 — `host:port` del database esterno a chi non è admin della macchina.**
   - **Raccomandata: sì, come oggi la riga Database della pagina dell'istanza.**
   - Alternativa: «su un'altra macchina», senza indirizzo.
4. **D4 — Firewall.**
   - **Raccomandata: DEFER**, finché non esiste una lettura senza root e senza le azioni on/apply/off.

## 17. Correzioni dopo la verifica del 22/11/2026

Ogni punto è nel testo qui sopra, al suo posto. In ordine di peso:

1. **Le durate si contano sull'orologio del server** (§4, §13): con il server in UTC e il browser a Roma, un job partito da 4 s diceva «in corso da 60 min» e un job finito non restava nella striscia.
2. **Il chip conta le istanze e le staging a parte** (§5 S1): le staging di una produzione di un'altra macchina erano contate come istanze.
3. **Una staging del cliente su un'altra macchina non si chiama più `localhost:20000`** (§5 S3, §7): «Staging del gg/mm», nessun «Apri Odoo» verso `localhost`, numerata dentro la sua produzione.
4. **Il nome del cliente non va alla macchina collegata** (§7, §13): la domanda è di `map`, e chiede solo quelle staging.
5. **Una macchina da aggiornare non è una macchina giù** (§5 S1, §6).
6. **Lo stato dei nomi si legge dal sito pubblicato** finché non c'è una pubblicazione nuova (§13), e la legenda ha la voce del lucchetto grigio.
7. **L'etichetta «N staging» sta fra i due riquadri**, sopra i nodi (§5 S1).
8. **Un job di 2 s aggiorna lo schema alla lettura dopo** (§11).
9. **Un collegamento lento non trattiene la mappa**: 5 s in tutto, 15 s nel browser, e «Adesso» non aspetta chi non ha risposto (§11). Lo stesso limite vale per gli elenchi `/api/instances?link=` e `/api/backups?link=`.
10. **`/instances` e `/api/instances` non dicono più al cliente il nome della macchina** (§7); lo stesso per `/backups` e `/api/backups`.
11. Minori: i conteggi dicono «(N qui)» quando lo schema ne mostra meno; la ricerca senza risultati dice «Nessuna istanza corrisponde a «…»»; le linee dei database hanno la loro corsia; i motivi sono in italiano; il contrasto dei grigi; l'elenco nomina anche ciò che lo schema piega e il titolo dello SVG è dello schema disegnato; «Sulla mappa» apre lo schema del workspace; la data della posta; il percorso in verticale nel pannello; un'azione rifatta bene toglie il suo avviso; le cache si svuotano di ciò che è stato rimosso.

Resta di Carlo la decisione **D3** (§16): oggi `host:port` del database esterno arriva ai tre ruoli del cliente, come la riga Database della pagina dell'istanza.

## Non verificato

- Se l'admin di un workspace vede la staging del suo workspace che sta su un'altra macchina: oggi `/stagings/elsewhere` risponde vuoto a chi non è admin della macchina. Va misurato su lab1 e lab2 prima della build.
- Se `database_line` per 60 ambienti sta nei 2 s dell'attesa. Non è misurato: se non ci sta, va in cache con la sua data.
- L'aspetto della disposizione verticale con 10 o più istanze a 375 px: va provato su un mockup in `docs/ux/mockups/`.

## Tracciabilità

Richiesta di Carlo del 22/11/2026, in chat. Non ci sono un `TS-NNN` né un `T<n>`/`HT<n>`.
- AD-003 e AD-004: il database su un'altra macchina e la replica.
- UX-006 §2 (schede dell'istanza) e §3 (date, nomi tecnici, `title`).
- UX-008 §2, regole 1 (primario), 2 (macchina che non risponde) e 3 (numero come filtro), e §3 (visibilità per persona).
- Fonti nel codice:
  - `/home/carlo/Projects/OmniaCLI/omniacli/app.py`: `WORKSPACE_ROUTES` (righe 275-340, `/instances` alla riga 320), `/api/servers` (1627), `/api/jobs` (2546), `/machines` (6813), `/api/instances` (8633);
  - `/home/carlo/Projects/OmniaCLI/omniacli/audit.py`: `/api/servers` e `/api/instances` in `NOT_SENSITIVE`, righe 62 e 86.

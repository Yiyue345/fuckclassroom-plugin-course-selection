use aes::cipher::{block_padding::Pkcs7, BlockDecryptMut, KeyIvInit};
use aes::Aes128;
use rand::distr::Alphanumeric;
use rand::Rng;
use rsteria2::DuplexStream;
use rsteria2::{connect, Config, HysteriaClient};
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{oneshot, Mutex, OnceCell};

type Aes128CbcDec = cbc::Decryptor<Aes128>;

// hy2 endpoint config — AES-128-CBC + PKCS7 ciphertexts compiled into the
// .rodata of the Rust dylib. The 16-byte AES key is split into four 4-byte
// segments stored as separately-named "tags" so a single grep / IDA scan
// over .rodata won't surface a contiguous key blob.
const TAG_A: [u8; 4] = [0x3e, 0x11, 0xac, 0x23];
const TAG_B: [u8; 4] = [0x0e, 0xfe, 0x02, 0x0a];
const TAG_C: [u8; 4] = [0x3b, 0xcf, 0xab, 0x77];
const TAG_D: [u8; 4] = [0x04, 0xfd, 0x63, 0x50];

const HY2_IV: [u8; 16] = [
    0xef, 0x18, 0x1d, 0xd4, 0x70, 0x9a, 0x39, 0x3a, 0x45, 0x5f, 0xeb, 0xa0, 0xba, 0xca, 0xa4, 0x0e,
];

const CT_SERVER: [u8; 32] = [
    0x49, 0x7a, 0x38, 0x5e, 0x22, 0xd6, 0x43, 0x25, 0x2f, 0x0f, 0x9c, 0x53, 0x1d, 0x1b, 0x8e, 0x22,
    0x08, 0x4f, 0x7e, 0x25, 0x7b, 0x1a, 0x06, 0x95, 0x96, 0xd9, 0x42, 0x78, 0xbd, 0xc9, 0x8d, 0xcc,
];
const CT_AUTH: [u8; 48] = [
    0x7f, 0x73, 0x83, 0xe0, 0x99, 0x3a, 0x47, 0xe7, 0x48, 0x8a, 0x5e, 0xd1, 0x7c, 0x66, 0xbc, 0x14,
    0x70, 0x37, 0x3f, 0x3b, 0xf7, 0x8a, 0x12, 0x26, 0xf2, 0x06, 0xcf, 0x7c, 0x4b, 0x8a, 0x5d, 0x67,
    0xa8, 0x65, 0xa2, 0xed, 0xb2, 0xe6, 0x02, 0xed, 0xe8, 0x5a, 0x82, 0x86, 0x22, 0xbc, 0x9c, 0xbf,
];
const CT_SNI: [u8; 16] = [
    0x9c, 0x36, 0x4b, 0xa4, 0x2f, 0x17, 0x7d, 0x44, 0xb6, 0x3b, 0x86, 0x21, 0x93, 0xa5, 0xb7, 0xce,
];

fn assemble_key() -> [u8; 16] {
    let mut k = [0u8; 16];
    k[0..4].copy_from_slice(&TAG_A);
    k[4..8].copy_from_slice(&TAG_B);
    k[8..12].copy_from_slice(&TAG_C);
    k[12..16].copy_from_slice(&TAG_D);
    k
}

fn decrypt_secret(ct: &[u8]) -> String {
    let mut buf = ct.to_vec();
    let plain = Aes128CbcDec::new(&assemble_key().into(), &HY2_IV.into())
        .decrypt_padded_mut::<Pkcs7>(&mut buf)
        .expect("hy2 secret: AES decrypt failed");
    String::from_utf8(plain.to_vec()).expect("hy2 secret: non-utf8 plaintext")
}

pub struct Hy2ProxyEndpoint {
    pub port: u16,
    pub username: String,
    pub password: String,
}

struct ProxyHandle {
    endpoint: Hy2ProxyEndpoint,
    _shutdown: oneshot::Sender<()>,
}

struct SharedClient {
    config: Config,
    client: Mutex<Arc<HysteriaClient>>,
}

impl SharedClient {
    async fn current(&self) -> Arc<HysteriaClient> {
        self.client.lock().await.clone()
    }

    /// Rebuild the underlying QUIC client. If `old` is `Some` and another
    /// caller already replaced the client, return the new one without
    /// reconnecting (deduplicates stampedes when many SOCKS connections see
    /// the same dead client at once).
    async fn reconnect(
        &self,
        old: Option<&Arc<HysteriaClient>>,
    ) -> anyhow::Result<Arc<HysteriaClient>> {
        let mut guard = self.client.lock().await;
        if let Some(old) = old {
            if !Arc::ptr_eq(&*guard, old) {
                return Ok(guard.clone());
            }
        }
        // Bound the connect() so a stuck QUIC handshake (e.g. iOS resume
        // where the old UDP socket is gone but the underlying state lingers)
        // doesn't hang the lock forever.
        let new = match tokio::time::timeout(CONNECT_TIMEOUT, connect(&self.config)).await {
            Ok(Ok(c)) => Arc::new(c),
            Ok(Err(e)) => return Err(e.into()),
            Err(_) => anyhow::bail!("hy2 reconnect timed out"),
        };
        *guard = new.clone();
        Ok(new)
    }
}

const CONNECT_TIMEOUT: Duration = Duration::from_secs(8);
const TCP_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// 上报给 hy2 服务端的下载速率(字节/秒,写入 `Hysteria-CC-RX`):
///   0           → 服务端用 BBR(自适应,对变化的移动网更稳)
///   > 0          → 服务端用 Brutal,按此速率「无视丢包」定速下发
///
/// 当前 = 0(BBR)。如需 Brutal,填实际下行(如 50Mbps = 6_250_000 B/s)。
const HY2_RX_BPS: u64 = 0;

/// 客户端上行(发送)拥塞控制速率(字节/秒):
///   0           → BBR
///   > 0          → 本地用 Brutal 按此速率定速上行
///
/// 上行主要是请求,量小,默认 0(BBR)即可。
const HY2_TX_BPS: u64 = 0;

static PROXY: OnceCell<ProxyHandle> = OnceCell::const_new();
static SHARED: OnceCell<Arc<SharedClient>> = OnceCell::const_new();

fn random_token(len: usize) -> String {
    rand::rng()
        .sample_iter(&Alphanumeric)
        .take(len)
        .map(char::from)
        .collect()
}

pub async fn hy2_start(insecure: bool) -> anyhow::Result<Hy2ProxyEndpoint> {
    let _ = rustls::crypto::ring::default_provider().install_default();

    let handle = PROXY
        .get_or_try_init(|| async {
            let server_addr = decrypt_secret(&CT_SERVER);
            let auth = decrypt_secret(&CT_AUTH);
            let sni = decrypt_secret(&CT_SNI);
            let sni_resolved = if sni.is_empty() {
                server_addr
                    .split(':')
                    .next()
                    .unwrap_or(&server_addr)
                    .to_string()
            } else {
                sni.clone()
            };
            let config = Config {
                auth,
                server_addr,
                server_name: sni_resolved,
                insecure,
                rx_bps: HY2_RX_BPS,
                tx_bps: HY2_TX_BPS,
                ..Default::default()
            };
            let client = Arc::new(connect(&config).await?);
            let shared = Arc::new(SharedClient {
                config,
                client: Mutex::new(client),
            });
            let _ = SHARED.set(shared.clone());
            let username = random_token(16);
            let password = random_token(32);
            start_tcp_listener(shared, username, password).await
        })
        .await?;
    Ok(Hy2ProxyEndpoint {
        port: handle.endpoint.port,
        username: handle.endpoint.username.clone(),
        password: handle.endpoint.password.clone(),
    })
}

/// Drop the cached QUIC client and rebuild it. Call this from Dart on
/// `AppLifecycleState.resumed` (especially on iOS) so the next request
/// doesn't have to wait for `tcp_connect` to time out before noticing
/// the old UDP socket is dead.
pub async fn hy2_force_reconnect() -> anyhow::Result<()> {
    let shared = match SHARED.get() {
        Some(s) => s.clone(),
        None => return Ok(()), // hy2 not started yet — nothing to do
    };
    shared.reconnect(None).await?;
    Ok(())
}

async fn start_tcp_listener(
    shared: Arc<SharedClient>,
    username: String,
    password: String,
) -> anyhow::Result<ProxyHandle> {
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let port = listener.local_addr()?.port();
    let (tx, mut rx) = oneshot::channel::<()>();

    let user = Arc::new(username.clone());
    let pass = Arc::new(password.clone());

    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = &mut rx => break,
                result = listener.accept() => {
                    if let Ok((stream, _)) = result {
                        let s = shared.clone();
                        let u = user.clone();
                        let p = pass.clone();
                        tokio::spawn(async move {
                            let _ = handle_socks5(stream, s, u, p).await;
                        });
                    }
                }
            }
        }
    });

    Ok(ProxyHandle {
        endpoint: Hy2ProxyEndpoint {
            port,
            username,
            password,
        },
        _shutdown: tx,
    })
}

/// Open a tcp stream over hy2 with bounded retries. Each failed attempt
/// rebuilds the underlying QUIC client (deduplicated across concurrent
/// callers via `SharedClient::reconnect`) before retrying. Backoff is
/// short and capped so a SOCKS client doesn't sit waiting forever.
async fn dial_with_retry(shared: &Arc<SharedClient>, target: &str) -> anyhow::Result<DuplexStream> {
    const MAX_ATTEMPTS: u32 = 4;
    let mut last_err: Option<anyhow::Error> = None;
    let mut last_client: Option<Arc<HysteriaClient>> = None;

    for attempt in 0..MAX_ATTEMPTS {
        if attempt > 0 {
            // 200ms, 400ms, 800ms — total worst-case ~1.4s before giving up.
            let backoff = Duration::from_millis(200u64 << (attempt - 1).min(3));
            tokio::time::sleep(backoff).await;
        }

        let client = match last_client.take() {
            Some(c) => c,
            None => shared.current().await,
        };

        // Wrap tcp_connect in a timeout. On iOS, after the app is suspended
        // and resumed, the QUIC connection's UDP socket has been silently
        // killed by the OS. The hysteria client doesn't notice immediately —
        // tcp_connect can hang indefinitely waiting for a stream that will
        // never open. Treat a timeout the same as a failure so the retry
        // loop can rebuild the client.
        let attempt_result =
            tokio::time::timeout(TCP_CONNECT_TIMEOUT, client.tcp_connect(target)).await;

        match attempt_result {
            Ok(Ok(s)) => return Ok(s),
            Ok(Err(e)) => {
                last_err = Some(e.into());
            }
            Err(_) => {
                last_err = Some(anyhow::anyhow!("hy2 tcp_connect timed out"));
            }
        }
        // Rebuild client; if reconnect itself fails, fall through to the
        // next iteration which will pick up whatever is current.
        if let Ok(fresh) = shared.reconnect(Some(&client)).await {
            last_client = Some(fresh);
        }
    }

    Err(last_err.unwrap_or_else(|| anyhow::anyhow!("hy2 dial failed")))
}

async fn handle_socks5(
    mut stream: TcpStream,
    shared: Arc<SharedClient>,
    expected_user: Arc<String>,
    expected_pass: Arc<String>,
) -> anyhow::Result<()> {
    let mut hdr = [0u8; 2];
    stream.read_exact(&mut hdr).await?;
    if hdr[0] != 0x05 {
        anyhow::bail!("unsupported SOCKS version: {}", hdr[0]);
    }
    let nmethods = hdr[1] as usize;
    let mut methods = vec![0u8; nmethods];
    stream.read_exact(&mut methods).await?;

    if !methods.contains(&0x02) {
        stream.write_all(&[0x05, 0xFF]).await?;
        anyhow::bail!("client did not offer username/password auth");
    }
    stream.write_all(&[0x05, 0x02]).await?;

    let mut auth_hdr = [0u8; 2];
    stream.read_exact(&mut auth_hdr).await?;
    if auth_hdr[0] != 0x01 {
        stream.write_all(&[0x01, 0x01]).await?;
        anyhow::bail!("unsupported auth subnegotiation version");
    }
    let ulen = auth_hdr[1] as usize;
    let mut uname = vec![0u8; ulen];
    stream.read_exact(&mut uname).await?;
    let mut plen_b = [0u8; 1];
    stream.read_exact(&mut plen_b).await?;
    let mut passwd = vec![0u8; plen_b[0] as usize];
    stream.read_exact(&mut passwd).await?;

    if uname.as_slice() != expected_user.as_bytes() || passwd.as_slice() != expected_pass.as_bytes()
    {
        stream.write_all(&[0x01, 0x01]).await?;
        anyhow::bail!("auth failed");
    }
    stream.write_all(&[0x01, 0x00]).await?;

    let mut req = [0u8; 4];
    stream.read_exact(&mut req).await?;

    let host = match req[3] {
        0x01 => {
            let mut ip = [0u8; 4];
            stream.read_exact(&mut ip).await?;
            format!("{}.{}.{}.{}", ip[0], ip[1], ip[2], ip[3])
        }
        0x03 => {
            let mut len = [0u8; 1];
            stream.read_exact(&mut len).await?;
            let mut domain = vec![0u8; len[0] as usize];
            stream.read_exact(&mut domain).await?;
            String::from_utf8(domain)?
        }
        0x04 => {
            let mut ip = [0u8; 16];
            stream.read_exact(&mut ip).await?;
            format!("[{}]", std::net::Ipv6Addr::from(ip))
        }
        t => anyhow::bail!("unknown ATYP: {}", t),
    };

    let mut port_b = [0u8; 2];
    stream.read_exact(&mut port_b).await?;
    let port = u16::from_be_bytes(port_b);

    let target = format!("{}:{}", host, port);
    let mut hy2 = dial_with_retry(&shared, &target).await?;

    stream
        .write_all(&[0x05, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        .await?;

    tokio::io::copy_bidirectional(&mut stream, &mut hy2).await?;
    Ok(())
}

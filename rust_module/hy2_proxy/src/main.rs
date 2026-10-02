// Python 服务启动的常驻 Hy2 SOCKS5 代理。
// run: cargo run --release --bin hy2_serve
use fuckclassroom_hy2::hy2_proxy::hy2_start;
use std::time::Duration;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .with_writer(std::io::stderr)
        .init();
    let ep = tokio::time::timeout(Duration::from_secs(20), hy2_start(false))
        .await
        .map_err(|_| anyhow::anyhow!("hy2_start timed out"))??;
    println!(
        "RUST_HY2_SOCKS port={} user={} pass={}",
        ep.port, ep.username, ep.password
    );
    loop {
        tokio::time::sleep(Duration::from_secs(3600)).await;
    }
}

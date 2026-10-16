//! Generates the gRPC client+server bindings for the vendored subset of
//! `proto/autoforward.proto` that this service implements.
//!
//! Only the `LogControlService` surface is vendored (see `proto/logger.proto`):
//! the package name, service name, message names, field names and field numbers
//! are byte-for-byte the ones the Python and Go clients already use, so the wire
//! contract (`/autoforward.LogControlService/Log`, ...) is unchanged.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-changed=proto/logger.proto");
    tonic_build::configure()
        .build_client(true)
        .build_server(true)
        .compile_protos(&["proto/logger.proto"], &["proto"])?;
    Ok(())
}

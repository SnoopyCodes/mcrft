package local.sumotraining;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;

/** Only immutable JSON strings cross into the Bukkit thread. No Bukkit calls here. */
final class TrainingSocketServer implements AutoCloseable {
    static final int MAX_LINE = 16384;
    private static final Gson JSON = new GsonBuilder().serializeNulls().create();
    static final class Request {
        final Peer peer;
        final JsonObject message;
        Request(Peer peer, JsonObject message) { this.peer = peer; this.message = message; }
    }

    static final class Peer implements AutoCloseable {
        private final Socket socket;
        private final BlockingQueue<String> outgoing = new ArrayBlockingQueue<>(256);
        volatile boolean open = true;
        boolean authenticated;
        Peer(Socket socket) { this.socket = socket; }
        void send(JsonObject message) {
            if (open && !outgoing.offer(JSON.toJson(message))) close();
        }
        void writeLoop() {
            try {
                BufferedWriter writer = new BufferedWriter(new OutputStreamWriter(
                    socket.getOutputStream(), StandardCharsets.UTF_8));
                while (open) {
                    String line = outgoing.poll(1, TimeUnit.SECONDS);
                    if (line != null) { writer.write(line); writer.write('\n'); writer.flush(); }
                }
            } catch (IOException | InterruptedException ignored) {
                // Reader and game thread observe open=false and abort the round.
            } finally { close(); }
        }
        public void close() {
            open = false;
            try { socket.close(); } catch (IOException ignored) { }
        }
    }

    private final ServerSocket listener;
    private final int timeoutMillis;
    private final AtomicReference<Peer> current = new AtomicReference<>();
    private final BlockingQueue<Request> incoming = new ArrayBlockingQueue<>(128);
    private volatile boolean open = true;

    TrainingSocketServer(int port, int timeoutSeconds) throws IOException {
        timeoutMillis = Math.multiplyExact(timeoutSeconds, 1000);
        if (timeoutMillis < 1000) throw new IllegalArgumentException("Read timeout must be positive");
        listener = new ServerSocket();
        listener.bind(new InetSocketAddress("127.0.0.1", port));
        daemon("sumo-accept", this::acceptLoop);
    }
    int port() { return listener.getLocalPort(); }
    Request poll() { return incoming.poll(); }

    private static void daemon(String name, Runnable runnable) {
        Thread thread = new Thread(runnable, name);
        thread.setDaemon(true);
        thread.start();
    }
    private void acceptLoop() {
        while (open) {
            try {
                Socket socket = listener.accept();
                socket.setTcpNoDelay(true);
                socket.setSoTimeout(timeoutMillis);
                Peer peer = new Peer(socket);
                Peer previous = current.get();
                if ((previous != null && previous.open) || !current.compareAndSet(previous, peer)) {
                    peer.close();
                    continue;
                }
                daemon("sumo-writer", peer::writeLoop);
                daemon("sumo-reader", () -> readLoop(peer));
            } catch (IOException ignored) {
                if (open) close();
            }
        }
    }
    private void readLoop(Peer peer) {
        try {
            Reader reader = new InputStreamReader(peer.socket.getInputStream(), StandardCharsets.UTF_8);
            StringBuilder line = new StringBuilder();
            int ch;
            while (peer.open && (ch = reader.read()) != -1) {
                if (ch == '\n') {
                    JsonObject message = new JsonParser().parse(line.toString()).getAsJsonObject();
                    if (!incoming.offer(new Request(peer, message))) break;
                    line.setLength(0);
                } else if (line.length() >= MAX_LINE) {
                    break;
                } else { line.append((char) ch); }
            }
        } catch (IOException | RuntimeException ignored) {
            // Malformed JSON, timeout, and EOF all close the session.
        } finally { peer.close(); }
    }
    public void close() {
        open = false;
        try { listener.close(); } catch (IOException ignored) { }
        Peer peer = current.get();
        if (peer != null) peer.close();
    }
}

package local.sumotraining;

import com.google.gson.JsonObject;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import org.junit.Test;
import static org.junit.Assert.*;

public class TrainingSocketServerTest {
    @Test public void fragmentedRequestsAreQueuedAndRepliesAreWrittenOffThread() throws Exception {
        try (TrainingSocketServer server = new TrainingSocketServer(0, 2);
             Socket socket = new Socket("127.0.0.1", server.port())) {
            socket.setSoTimeout(2000);
            OutputStream out = socket.getOutputStream();
            out.write("{\"op\":\"hel".getBytes(StandardCharsets.UTF_8)); out.flush();
            out.write("lo\",\"id\":1}\n".getBytes(StandardCharsets.UTF_8)); out.flush();
            TrainingSocketServer.Request request = null;
            long deadline = System.nanoTime() + 2000000000L;
            while (request == null && System.nanoTime() < deadline) {
                request = server.poll(); Thread.sleep(2);
            }
            assertNotNull(request);
            assertEquals("hello", request.message.get("op").getAsString());
            JsonObject reply = new JsonObject(); reply.addProperty("ok", true);
            request.peer.send(reply);
            BufferedReader reader = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
            assertEquals("{\"ok\":true}", reader.readLine());
        }
    }
    @Test public void malformedOrOversizedMessagesDisconnect() throws Exception {
        for (String message : new String[]{"not-json\n", new String(new char[TrainingSocketServer.MAX_LINE + 1]).replace('\0', 'x')}) {
            try (TrainingSocketServer server = new TrainingSocketServer(0, 2);
                 Socket socket = new Socket("127.0.0.1", server.port())) {
                socket.setSoTimeout(2000);
                socket.getOutputStream().write(message.getBytes(StandardCharsets.UTF_8));
                socket.getOutputStream().flush();
                assertEquals(-1, socket.getInputStream().read());
            }
        }
    }
}

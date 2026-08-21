// A real Node server. Run it with real Node, or let Devbox's fake runtime stand
// in for it in CI — see server.js.fake next to this file.
const http = require('node:http');

const port = Number(process.env.PORT || 3002);
const name = process.env.SERVICE_NAME || 'modern-api';

const server = http.createServer((request, response) => {
  if (request.url === '/health') {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ status: 'ok', service: name, node: process.version }));
    return;
  }
  response.writeHead(200, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify({ service: name, node: process.version }));
});

server.listen(port, '127.0.0.1', () => {
  console.log(`${name} ${process.version} listening on 127.0.0.1:${port}`);
});

for (const signal of ['SIGTERM', 'SIGINT', 'SIGBREAK']) {
  process.on(signal, () => {
    console.log(`${name}: received ${signal}, shutting down`);
    server.close(() => process.exit(0));
  });
}

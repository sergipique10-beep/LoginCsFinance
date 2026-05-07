# Servidor de desarrollo HTTPS local
#
# Requisitos previos (una sola vez por máquina):
#   1. Instalar mkcert:
#        winget install FiloSottile.mkcert
#   2. Instalar la CA raíz local en el almacén del sistema:
#        mkcert -install
#   3. Generar los certificados para este proyecto (ejecutar desde la raíz del repo):
#        mkdir certs
#        mkcert -cert-file certs/localhost.pem -key-file certs/localhost-key.pem localhost 127.0.0.1
#
# La carpeta certs/ está en .gitignore — nunca commitear los certificados.
#
# Arrancar con:
#   python run_dev.py

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8001,
        reload=True,
    )

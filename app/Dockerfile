# Use Node 18
FROM node:18

# Set working directory
WORKDIR /app

# Copy ONLY package files first (for caching)
COPY package*.json ./

# Install dependencies
RUN npm install

# NOW copy ALL source code (including server.js)
COPY . .

# Expose port
EXPOSE 3000

# Start app
CMD ["node", "server.js"]
CREATE DATABASE IF NOT EXISTS `youtube_converter` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS `auction_radar` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON `youtube_converter`.* TO 'devuser'@'%';
GRANT ALL PRIVILEGES ON `auction_radar`.* TO 'devuser'@'%';
FLUSH PRIVILEGES;

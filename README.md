# Library Simplified Metadata Server

This is the Metadata Server for [Library Simplified](http://www.librarysimplified.org/). The metadata server utilizes and intelligently almagamates a wide variety of information sources for library ebooks and incorporates them into the reading experience for users, improving selection, search, and recommendations.

## Installation

Thorough deployment instructions, including essential libraries for Linux systems, can be found [in the Library Simplified wiki](https://github.com/NYPL-Simplified/Simplified/wiki/Deployment-Instructions).

Keep in mind that the metadata server requires unique database names and a data directory, as demonstrated below.
```sh
$ sudo -u postgres psql
CREATE DATABASE simplified_metadata_test;
CREATE DATABASE simplified_metadata_dev;

CREATE USER simplified with password '[password]';
grant all privileges on database simplified_metadata_dev to simplified;

CREATE USER simplified_test with password '[password]';
grant all privileges on database simplified_metadata_test to simplified_test;
```

```sh
$ git clone https://github.com/NYPL/Simplified-data.git YOUR_DATA_DIRECTORY
```

YOUR_DATA_DIRECTORY should be replaced with the directory you specified as "data_directory" in your configuration file.
